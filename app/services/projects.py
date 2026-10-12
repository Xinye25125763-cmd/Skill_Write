"""项目与需求服务：CRUD + brief 修订历史 + 追问预算。"""
import json
import shutil
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.ulid import new_ulid
from app.models import (
    AgentRun,
    BriefRevision,
    EvalCaseResult,
    EvalRun,
    Job,
    Project,
    SkillVersion,
    Suite,
    utcnow,
    WorkflowEvent,
)
from app.services import queue
from app.services.brief_schema import (
    BriefDraft,
    BriefValidationError,
    SourceType,
    FieldSource,
    validate_brief,
)

# 细化方案 §5：最多连续 2 轮追问，之后未决项标记 unresolved 继续推进
MAX_FOLLOWUP_ROUNDS = 2


class ProjectBusyError(RuntimeError):
    """运行中的任务必须先结束，才可删除项目。"""


class ProjectCreate(BaseModel):
    """创建项目请求。"""

    name: str = Field(min_length=1, max_length=200)
    target_platform: str = "catpaw"


class ProjectInfo(BaseModel):
    """项目视图。"""

    id: str
    name: str
    target_platform: str
    created_at: str
    latest_brief_revision_id: str | None = None


class BriefImport(BaseModel):
    """人工导入 brief（S2 降级路径，细化方案 §8 S2）。

    模型输出在别处生成后粘贴于此；系统只做结构化校验与业务校验，
    不发起任何模型调用。
    """

    brief: dict  # BriefPayload 形状的原始 JSON


class BriefService:
    """brief 修订与追问状态管理。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def import_draft(self, project_id: str, raw: dict, revision_id: str | None = None) -> tuple[str, BriefDraft, list[str]]:
        """导入一份 brief 草案（人工粘贴路径），落库为 draft 状态修订。

        返回 (修订 ID, 草案视图, 业务校验问题清单)。
        校验失败不阻断落库——问题驱动追问（主§5.2 待确认列）。
        """
        try:
            draft = BriefDraft(brief=raw)  # type: ignore[arg-type]
        except Exception as exc:  # 结构校验失败 → parse_failed，转人工编辑
            raise BriefValidationError([f"parse_failed: {exc}"]) from exc

        issues = validate_brief(draft)

        rev_id = new_ulid()
        rev = BriefRevision(
            id=rev_id,
            project_id=project_id,
            parent_revision_id=revision_id,
            brief_json=json.dumps(raw, ensure_ascii=False),
            status="draft",
            created_at=utcnow(),
        )
        self.session.add(rev)
        self.session.commit()
        return rev_id, draft, issues

    def confirm(self, project_id: str, revision_id: str, edits: dict | None = None) -> tuple[str, BriefDraft, list[str]]:
        """确认一份 draft 修订：应用编辑、改写来源为 user_confirmed、置 confirmed。

        编辑以字段路径为键（如 "purpose"、"trigger_conditions[0]"），
        仅允许改值与确认来源，不允许凭空新增来源。
        """
        rev = (
            self.session.query(BriefRevision)
            .filter_by(id=revision_id, project_id=project_id)
            .one()
        )
        raw = json.loads(rev.brief_json)
        if edits:
            _apply_edits(raw, edits)

        # 用户确认：非附件来源字段视为 user_confirmed；
        # 附件来源字段必须经显式确认（edits 中出现过该路径）才转 confirmed。
        confirmed_paths = set(edits or {})
        _mark_confirmed(raw, confirmed_paths)

        draft = BriefDraft(brief=raw)  # type: ignore[arg-type]
        issues = validate_brief(draft)

        # 确认产生新修订行（不可变历史，主§5.2 需求修订保留历史）
        new_id = new_ulid()
        self.session.add(
            BriefRevision(
                id=new_id,
                project_id=project_id,
                parent_revision_id=revision_id,
                brief_json=json.dumps(raw, ensure_ascii=False),
                status="confirmed",
                created_at=utcnow(),
            )
        )
        self.session.commit()
        return new_id, draft, issues


def _apply_edits(raw: dict, edits: dict) -> None:
    """按路径应用编辑：路径必须已存在，不允许新增未知字段。

    带来源字段（dict 含 value/source）只改 value，保留来源结构；
    纯值字段直接赋值。
    """
    for path, value in edits.items():
        parts = _split_path(path)
        target = raw
        for part in parts[:-1]:
            target = target[part]
        leaf = parts[-1]
        if isinstance(target, list):
            item = target[int(leaf)]
            if isinstance(item, dict) and "value" in item:
                item["value"] = value
            else:
                target[int(leaf)] = value
        else:
            if leaf not in target:
                msg = f"unknown field path: {path}"
                raise BriefValidationError([msg])
            current = target[leaf]
            if isinstance(current, dict) and "value" in current:
                current["value"] = value
            else:
                target[leaf] = value


def _mark_confirmed(raw: dict, confirmed_paths: set[str]) -> None:
    """来源改写：全部待确认字段的 source.type → user_confirmed。

    S2 简化实现：confirm 动作本身即用户确认（服务端已鉴权），
    附件来源字段只有在显式编辑过时才转 confirmed，否则保持 attachment_text
    并进 unresolved——与 brief_schema 的强制待确认规则一致。
    """
    def walk(node) -> None:  # noqa: ANN001
        if isinstance(node, dict):
            if "source" in node and "value" in node:
                stype = node["source"].get("type")
                if stype == "attachment_text":
                    return  # 保持待确认
                node["source"]["type"] = SourceType.USER_CONFIRMED.value
            else:
                for child in node.values():
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(raw)
    # 显式确认过的附件字段：单独转 confirmed
    for path in confirmed_paths:
        parts = _split_path(path)
        target = raw
        for part in parts[:-1]:
            target = target[part]
        leaf = parts[-1]
        item = target[int(leaf)] if isinstance(target, list) else target[leaf]
        if isinstance(item, dict) and "source" in item:
            item["source"]["type"] = SourceType.USER_CONFIRMED.value


def _split_path(path: str) -> list:
    """拆 'trigger_conditions[0]' → ['trigger_conditions', 0]。"""
    import re

    parts: list = []
    for m in re.finditer(r"([^.\[\]]+)|\[(\d+)\]", path):
        if m.group(1):
            parts.append(m.group(1))
        else:
            parts.append(int(m.group(2)))
    return parts


class ProjectService:
    """项目 CRUD。"""

    def __init__(self, session: Session, data_dir: Path | None = None) -> None:
        self.session = session
        # F04：删除项目需清理磁盘数据目录；未显式传入时取当前配置
        from app.core.config import load_config

        self.data_dir = data_dir if data_dir is not None else load_config().data_dir

    def create(self, req: ProjectCreate) -> Project:
        """创建项目：名称非空，平台需已注册。"""
        from app.services.brief_schema import REGISTERED_PLATFORMS

        if req.target_platform not in REGISTERED_PLATFORMS:
            raise BriefValidationError([f"target_platform 未注册：{req.target_platform}"])
        project = Project(id=new_ulid(), name=req.name, target_platform=req.target_platform, created_at=utcnow())
        self.session.add(project)
        self.session.commit()
        return project

    def get(self, project_id: str) -> Project | None:
        return self.session.get(Project, project_id)

    def list_projects(self) -> list[Project]:
        return self.session.query(Project).order_by(Project.created_at.desc()).all()

    def delete(self, project_id: str) -> bool:
        """短写事务阻止同时领取任务；文件暂存后提交，失败可恢复。"""
        self.cleanup_pending = False
        self.session.execute(text("BEGIN IMMEDIATE"))
        project = self.get(project_id)
        if project is None:
            self.session.rollback()
            return False
        jobs = [j for j in self.session.query(Job).all()
                if json.loads(j.payload_json or "{}").get("project_id") == project_id]
        if any(j.status == "running" for j in jobs):
            self.session.rollback()
            raise ProjectBusyError("项目有运行中的任务，请取消并等待任务结束后再删除")
        project_dir = self.data_dir / "projects" / project_id
        tombstone = self.data_dir / ".trash" / f"{project_id}.{new_ulid()}"
        staged = False
        try:
            if project_dir.exists():
                tombstone.parent.mkdir(parents=True, exist_ok=True)
                project_dir.rename(tombstone)
                staged = True
            for job in jobs:
                if job.status == "queued":
                    job.status = "cancelled"
                    job.cancel_requested = 1
                    job.updated_at = queue.now()
                    job.error_json = json.dumps({"code": "project_deleted", "message": "项目已删除"})
                    self.session.add(WorkflowEvent(job_id=job.id, type="job_cancelled",
                        stage="done", message="项目删除，排队任务取消", ts=queue.now()))
            # ② 子表清理（全部在同一事务，失败整体回滚）
            eval_run_ids = [
                r[0] for r in self.session.query(EvalRun.id).filter_by(project_id=project_id).all()
            ]
            if eval_run_ids:
                self.session.query(EvalCaseResult).filter(
                    EvalCaseResult.eval_run_id.in_(eval_run_ids)
                ).delete(synchronize_session=False)
            self.session.query(EvalRun).filter_by(project_id=project_id).delete(synchronize_session=False)
            self.session.query(AgentRun).filter_by(project_id=project_id).delete(synchronize_session=False)
            self.session.query(Suite).filter_by(project_id=project_id).delete(synchronize_session=False)
            # skill_versions：先解除 brief_revision_id / parent_version_id 引用再删
            self.session.query(SkillVersion).filter_by(project_id=project_id).update(
                {"brief_revision_id": None, "parent_version_id": None},
                synchronize_session=False,
            )
            self.session.query(SkillVersion).filter_by(project_id=project_id).delete(synchronize_session=False)
            self.session.query(BriefRevision).filter_by(project_id=project_id).delete(synchronize_session=False)

            self.session.delete(project)
            self.session.commit()

        except Exception:
            self.session.rollback()
            if staged:
                tombstone.rename(project_dir)
            raise
        if staged:
            try:
                shutil.rmtree(tombstone)
            except OSError:
                self.cleanup_pending = True
        return True

    def latest_brief_revision(self, project_id: str) -> BriefRevision | None:
        return (
            self.session.query(BriefRevision)
            .filter_by(project_id=project_id)
            .order_by(BriefRevision.created_at.desc())
            .first()
        )


def project_info(project: Project, latest_rev: BriefRevision | None) -> ProjectInfo:
    """Project → 视图。"""
    return ProjectInfo(
        id=project.id,
        name=project.name,
        target_platform=project.target_platform,
        created_at=project.created_at.isoformat(),
        latest_brief_revision_id=latest_rev.id if latest_rev else None,
    )


def reconcile_project_trash(cfg) -> dict:
    """启动时修复删除的崩溃窗口；原位置冲突时保留待人工处理的证据。"""
    import re
    from app.core.db import create_db_engine, db_path
    status = {"restored": 0, "removed": 0, "pending": 0}
    engine = create_db_engine(db_path(cfg))
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            trash = cfg.data_dir / ".trash"
            for entry in list(trash.iterdir()) if trash.exists() else []:
                if entry.is_symlink() or not entry.is_dir() or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}\.[0-9A-HJKMNP-TV-Z]{26}", entry.name):
                    status["pending"] += 1
                    continue
                pid = entry.name.split(".")[0]
                root = cfg.data_dir / "projects" / pid
                try:
                    if session.get(Project, pid) is not None:
                        if root.exists():
                            status["pending"] += 1
                        else:
                            root.parent.mkdir(parents=True, exist_ok=True)
                            entry.rename(root)
                            status["restored"] += 1
                    else:
                        shutil.rmtree(entry)
                        status["removed"] += 1
                except OSError:
                    status["pending"] += 1
            session.commit()
    finally:
        engine.dispose()
    return status
