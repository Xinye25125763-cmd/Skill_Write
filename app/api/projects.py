"""项目与需求路由（细化方案 §3.2 的 S2 子集）。"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import create_db_engine, db_path
from app.core.config import load_config
from app.services.brief_schema import BriefValidationError
from app.services.projects import (
    BriefImport,
    BriefService,
    ProjectCreate,
    ProjectService,
    ProjectBusyError,
    project_info,
)

router = APIRouter(prefix="/api")

_engines: dict = {}


def get_session() -> Session:
    """请求级会话：按数据库路径缓存引擎（路径变更自动失效，测试隔离需要）。"""
    from sqlalchemy.orm import sessionmaker

    db_file = str(db_path(load_config()))
    if db_file not in _engines:
        _engines.clear()  # 单库应用：新路径即全局换库
        _engines[db_file] = create_db_engine(Path(db_file))
    session = sessionmaker(bind=_engines[db_file])()
    try:
        yield session
    finally:
        session.close()


def _brief_error(exc: BriefValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "validation_failed", "errors": exc.errors})


@router.post("/projects", status_code=201)
def create_project(req: ProjectCreate, session: Session = Depends(get_session)) -> dict:
    """创建项目。"""
    try:
        project = ProjectService(session).create(req)
    except BriefValidationError as exc:
        raise _brief_error(exc) from exc
    return project_info(project, None).model_dump()


@router.get("/projects")
def list_projects(session: Session = Depends(get_session)) -> list[dict]:
    """项目列表（含最近需求修订摘要，主§5.1 首页）。"""
    svc = ProjectService(session)
    out = []
    for project in svc.list_projects():
        rev = svc.latest_brief_revision(project.id)
        out.append(project_info(project, rev).model_dump())
    return out


@router.get("/projects/{project_id}")
def get_project(project_id: str, session: Session = Depends(get_session)) -> dict:
    project = ProjectService(session).get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    rev = ProjectService(session).latest_brief_revision(project.id)
    return project_info(project, rev).model_dump()


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, response: Response, session: Session = Depends(get_session)) -> None:
    svc = ProjectService(session)
    try:
        if not svc.delete(project_id):
            raise HTTPException(status_code=404, detail={"code": "not_found"})
    except ProjectBusyError as exc:
        raise HTTPException(status_code=409, detail={"code": "project_busy", "message": str(exc)}) from exc
    if svc.cleanup_pending:
        response.headers["X-Cleanup-Pending"] = "true"


@router.post("/projects/{project_id}/briefs", status_code=201)
def import_brief(
    project_id: str,
    req: BriefImport,
    session: Session = Depends(get_session),
) -> dict:
    """导入 brief 草案（S2 降级路径：人工粘贴模型输出）。

    返回三层分列视图与业务校验问题（待确认驱动追问）。
    """
    project = ProjectService(session).get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    svc = BriefService(session)
    latest = svc.session.query(
        __import__("app.models", fromlist=["BriefRevision"]).BriefRevision
    ).filter_by(project_id=project_id).order_by(
        __import__("app.models", fromlist=["BriefRevision"]).BriefRevision.created_at.desc()
    ).first()
    try:
        rev_id, draft, issues = svc.import_draft(project_id, req.brief, latest.id if latest else None)
    except BriefValidationError as exc:
        raise _brief_error(exc) from exc
    return {
        "revision_id": rev_id,
        "status": "draft",
        "explicit": draft.explicit,
        "inferred": draft.inferred,
        "to_confirm": draft.to_confirm,
        "issues": issues,
    }


@router.patch("/projects/{project_id}/briefs/{revision_id}")
def confirm_brief(
    project_id: str,
    revision_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    """确认 draft 修订：可附字段编辑 {"edits": {"path": "value"}}。"""
    from app.models import BriefRevision

    if (
        session.query(BriefRevision)
        .filter_by(id=revision_id, project_id=project_id)
        .one_or_none()
        is None
    ):
        raise HTTPException(status_code=404, detail={"code": "not_found"})

    svc = BriefService(session)
    edits = body.get("edits") or None
    try:
        new_id, draft, issues = svc.confirm(project_id, revision_id, edits)
    except BriefValidationError as exc:
        raise _brief_error(exc) from exc
    return {
        "revision_id": new_id,
        "status": "confirmed",
        "explicit": draft.explicit,
        "inferred": draft.inferred,
        "to_confirm": draft.to_confirm,
        "issues": issues,
    }


@router.get("/projects/{project_id}/briefs")
def list_briefs(project_id: str, session: Session = Depends(get_session)) -> list[dict]:
    """修订历史（主§5.2：需求修订保留历史）。"""
    from app.models import BriefRevision

    rows = (
        session.query(BriefRevision)
        .filter_by(project_id=project_id)
        .order_by(BriefRevision.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "parent_revision_id": r.parent_revision_id,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/projects/{project_id}/briefs/{revision_id}")
def get_brief(project_id: str, revision_id: str, session: Session = Depends(get_session)) -> dict:
    """读取单份修订的完整 brief JSON。"""
    from app.models import BriefRevision

    rev = session.query(BriefRevision).filter_by(id=revision_id, project_id=project_id).one_or_none()
    if rev is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return {
        "id": rev.id,
        "status": rev.status,
        "created_at": rev.created_at.isoformat(),
        "brief": json.loads(rev.brief_json),
    }
