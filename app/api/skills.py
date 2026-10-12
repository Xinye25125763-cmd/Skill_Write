"""技能草稿与版本路由（细化方案 §3.2 的 S3/S4 子集）。

S4 起版本创建改为异步任务（202 + job_id），由 Worker 执行。
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.projects import get_session
from app.models import SkillVersion
from app.services import queue
from app.services.skill_files import (
    FileValidationError,
    SkillFileService,
    version_view,
)
from app.core.config import load_config

router = APIRouter(prefix="/api")


def _svc(session: Session) -> SkillFileService:
    return SkillFileService(load_config(), session)


def _file_error(exc: FileValidationError) -> HTTPException:
    detail = {"code": "validation_failed", "errors": exc.errors}
    if exc.warnings:
        detail["warnings"] = exc.warnings
    return HTTPException(status_code=422, detail=detail)


class FileWrite(BaseModel):
    """写草稿文件请求。"""

    content: str = Field(max_length=1_000_000)


class VersionCreate(BaseModel):
    """创建版本请求。"""

    label: str | None = Field(default=None, max_length=200)
    parent_version_id: str | None = None


@router.get("/projects/{project_id}/skills/draft")
def list_draft(project_id: str, session: Session = Depends(get_session)) -> dict:
    """草稿文件树。"""
    return {"files": _svc(session).list_draft(project_id)}


@router.get("/projects/{project_id}/skills/draft/files/{file_path:path}")
def read_draft(project_id: str, file_path: str, session: Session = Depends(get_session)) -> dict:
    """读草稿单文件。"""
    try:
        content = _svc(session).read_draft(project_id, file_path)
    except FileValidationError as exc:
        raise _file_error(exc) from exc
    return {"path": file_path, "content": content}


@router.put("/projects/{project_id}/skills/draft/files/{file_path:path}")
def write_draft(
    project_id: str,
    file_path: str,
    req: FileWrite,
    session: Session = Depends(get_session),
) -> dict:
    """写草稿文件（自动保存语义）。"""
    try:
        return _svc(session).write_draft(project_id, file_path, req.content)
    except FileValidationError as exc:
        raise _file_error(exc) from exc


@router.delete("/projects/{project_id}/skills/draft/files/{file_path:path}", status_code=204)
def delete_draft(project_id: str, file_path: str, session: Session = Depends(get_session)) -> None:
    """删草稿文件。"""
    try:
        _svc(session).delete_draft(project_id, file_path)
    except FileValidationError as exc:
        raise _file_error(exc) from exc


@router.post("/projects/{project_id}/skills/versions", status_code=202)
def create_version(
    project_id: str,
    req: VersionCreate,
    session: Session = Depends(get_session),
) -> dict:
    """从草稿创建不可变版本：入队异步任务，返回 job_id（主§10 异步约定）。

    校验/冲突由 Worker 执行并结构化落在 job.error 中；
    结果（版本视图）在 job.result。
    """
    job = queue.enqueue(
        session,
        "version_create",
        {
            "project_id": project_id,
            "label": req.label,
            "parent_version_id": req.parent_version_id,
        },
    )
    return {"job_id": job.id, "status": job.status}


@router.get("/projects/{project_id}/skills/versions")
def list_versions(project_id: str, session: Session = Depends(get_session)) -> list[dict]:
    """版本列表（含哈希、父版本、警告）。"""
    return [_version_view_local(v) for v in _svc(session).list_versions(project_id)]


@router.get("/projects/{project_id}/skills/versions/{version_id}")
def get_version(project_id: str, version_id: str, session: Session = Depends(get_session)) -> dict:
    version = _svc(session).get_version(project_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return version_view(version, with_files=True)


@router.get("/projects/{project_id}/skills/versions/{version_id}/files/{file_path:path}")
def read_version_file(
    project_id: str, version_id: str, file_path: str, session: Session = Depends(get_session)
) -> dict:
    """读版本内文件（带哈希完整性校验）。"""
    try:
        content = _svc(session).read_version_file(project_id, version_id, file_path)
    except FileValidationError as exc:
        raise _file_error(exc) from exc
    return {"path": file_path, "content": content}


def _version_view_local(v: SkillVersion) -> dict:
    """列表视图（不含文件清单）。"""
    return version_view(v)
