"""version_create 处理器：从草稿创建不可变版本（原 S3 同步逻辑迁入异步）。

错误分类落库（细化方案 §4）：
    conflict → error.code=conflict + existing_version_id；
    validation_failed → error.errors 列表。
"""
from app.services.skill_files import (
    FileValidationError,
    SkillFileService,
    VersionConflictError,
    version_view,
)
from app.models import BriefRevision
from app.workers.handlers import HandlerContext, payload_of, register


@register("version_create")
def handle(session, cfg, job, ctx: HandlerContext) -> dict:  # noqa: ANN001
    payload = payload_of(job)
    project_id = payload["project_id"]
    ctx.event("validate", "校验草稿文件树")

    latest_brief = (
        session.query(BriefRevision)
        .filter_by(project_id=project_id, status="confirmed")
        .order_by(BriefRevision.created_at.desc())
        .first()
    )

    svc = SkillFileService(cfg, session)
    version = svc.create_version(
        project_id,
        parent_version_id=payload.get("parent_version_id"),
        brief_revision_id=latest_brief.id if latest_brief else None,
        label=payload.get("label"),
    )

    ctx.event("created", f"版本 {version.id} 已创建")
    return version_view(version, with_files=True)
