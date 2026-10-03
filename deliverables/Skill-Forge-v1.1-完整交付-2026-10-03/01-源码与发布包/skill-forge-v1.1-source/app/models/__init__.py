"""数据库模型：SQLAlchemy 声明式模型，与迁移一一对应（主§7）。"""
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """统一 UTC 时间戳来源。"""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """声明式基类。"""


class Project(Base):
    """项目（主§7 projects 表）。内部 ID 与可编辑名称分离。"""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # ULID
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target_platform: Mapped[str] = mapped_column(Text, nullable=False, default="catpaw")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class BriefRevision(Base):
    """需求版本（主§7 brief_revisions 表）：不可变，修订生成新行。"""

    __tablename__ = "brief_revisions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # ULID
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("brief_revisions.id"), nullable=True
    )
    brief_json: Mapped[str] = mapped_column(Text, nullable=False)  # 完整结构化需求（含来源标注）
    status: Mapped[str] = mapped_column(Text, nullable=False, default="confirmed")
    # status: draft | confirmed
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class SkillVersion(Base):
    """技能版本（主§7 skill_versions 表）：不可变快照。

    - content_hash：全文件树规范化哈希（路径排序 + 文件哈希拼接再哈希）；
    - files_json：清单 [{path, hash, size, kind}]；
    - 内部 ULID 与可编辑 label 分离（主§8）。
    """

    __tablename__ = "skill_versions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("skill_versions.id"), nullable=True
    )
    brief_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("brief_revisions.id"), nullable=True
    )
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    files_json: Mapped[str] = mapped_column(Text, nullable=False)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="validated")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AgentRun(Base):
    """执行记录（主§7 run_results 的 v0 子集，S5）。

    一次 agent_run 任务对应一行；steps_json 为工具循环轨迹，
    sandbox_files_json 为 F09 结构 {"files": [...清单含二进制...],
    "text_contents": {...仅文本内容...}}（供 S6 断言评分）。
    """

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # ULID
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL = 无技能基线
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")  # running|finished
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    sandbox_files_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    budget_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sandbox_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(nullable=False)
    finished_at: Mapped[float | None] = mapped_column(nullable=True)


class Suite(Base):
    """测试集（S6 最小版）：用例 + 程序断言，不可变（创建后不改）。"""

    __tablename__ = "suites"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # ULID
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cases_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(nullable=False)


class EvalRun(Base):
    """对照评测运行（S6）：候选版本 vs 无技能基线，逐用例执行并评分。"""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    suite_id: Mapped[str] = mapped_column(
        ForeignKey("suites.id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[str] = mapped_column(Text, nullable=False)  # 候选技能版本
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(nullable=False)
    finished_at: Mapped[float | None] = mapped_column(nullable=True)


class EvalCaseResult(Base):
    """逐例结果（S6）：执行状态与评分状态分离存储与展示。"""

    __tablename__ = "eval_case_results"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[str] = mapped_column(Text, nullable=False)
    group_name: Mapped[str] = mapped_column(Text, nullable=False)  # candidate | baseline
    agent_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    exec_status: Mapped[str] = mapped_column(Text, nullable=False)  # executed | exec_failed | skipped
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_status: Mapped[str] = mapped_column(Text, nullable=False, default="not_scored")
    assertions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    passed_count: Mapped[int] = mapped_column(default=0)
    total_count: Mapped[int] = mapped_column(default=0)
    score_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(nullable=False)


class Job(Base):
    """持久化任务（主§9 / 细化方案 §4.1）。

    状态流转：queued → running → (succeeded | failed | cancelled | interrupted)。
    幂等键唯一索引 (type, idempotency_key)，SQLite 中 NULL 互不冲突。
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[float | None] = mapped_column(nullable=True)
    heartbeat_at: Mapped[float | None] = mapped_column(nullable=True)
    cancel_requested: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[float] = mapped_column(nullable=False)
    updated_at: Mapped[float] = mapped_column(nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkflowEvent(Base):
    """单调事件流（主§10 SSE 补取源，细化方案 §3.3）。"""

    __tablename__ = "workflow_events"

    event_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)  # job_created|progress|job_succeeded|job_failed|job_cancelled
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    progress_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[float] = mapped_column(nullable=False)
