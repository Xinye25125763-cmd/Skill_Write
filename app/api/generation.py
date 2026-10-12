"""生成流程 API（工程计划 §3，T03 + T08 异步接入）。

- POST /api/projects/{pid}/generation-flows：202 异步受理（flow_id + 首阶段 job_id）；
- GET  /api/generation-flows/{flow_id}：快照视图；
- POST .../answers：AWAITING_INPUT 时回答（revision CAS + 幂等键），重排 extract；
- POST .../resume：INTERRUPTED 恢复，按检查点重排当前阶段；
- POST .../cancel：取消传播到当前/排队阶段。

处理器不阻塞等待用户；重复 answers 被 not_awaiting_input 状态闸挡下，
不重复触发生成。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.projects import get_session
from app.core.config import load_config
from app.generation import budget as budget_svc
from app.generation import flow as flow_store
from app.generation.contracts import (
    CheckReport,
    CheckStatus,
    DeliveryReceipt,
    DeliveryStatus,
    FlowState,
    RequirementContract,
)
from app.generation.delivery import DeliveryError, package_delivery
from app.generation.evaluation import evaluate_candidate
from app.models import GenerationFlow, Project, SkillVersion
from app.platforms.base import get_adapter
from app.services import queue as queue_svc

router = APIRouter(prefix="/api")


class GenerationFlowCreate(BaseModel):
    """创建请求：自然语言需求 + 目标宿主 + 资源 + 规则版本 + 预算。"""

    request_text: str = Field(min_length=1)
    target_platform: str = "codex"
    resource_ids: list[str] = Field(default_factory=list)
    profile_revision: int | None = None
    budget: dict | None = None


class AnswerInput(BaseModel):
    """回答/恢复输入：带 revision（CAS）与幂等键。"""

    revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)
    answers: dict = Field(default_factory=dict)


def _flow_view(snap: flow_store.FlowSnapshot, job_id: str | None = None) -> dict:
    return {
        "flow_id": snap.flow_id,
        "project_id": snap.project_id,
        "state": snap.state.value if isinstance(snap.state, FlowState) else str(snap.state),
        "revision": snap.revision,
        "budget": snap.budget.model_dump(),
        "checkpoint": snap.checkpoint,
        "pending_questions": snap.pending_questions,
        "error": snap.error,
        "job_id": job_id,
    }


def _http_409(exc: flow_store.StaleRevision) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "stale_revision",
            "message": str(exc),
            "expected": exc.expected,
            "actual": exc.actual,
        },
    )


@router.post("/projects/{pid}/generation-flows", status_code=202)
def create_generation_flow(
    pid: str, body: GenerationFlowCreate, session: Session = Depends(get_session)
) -> dict:
    """创建生成流程并排队首个阶段（extract）。"""
    if session.get(Project, pid) is None:
        raise HTTPException(status_code=404, detail="project not found")

    request = body.model_dump()
    try:
        snap = flow_store.create_flow(session, project_id=pid, request=request)
        job = flow_store.run_stage_once(session, flow_id=snap.flow_id, stage="extract")
    except flow_store.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _flow_view(snap, job_id=job.id)


@router.get("/generation-flows/{flow_id}")
def get_generation_flow(flow_id: str, session: Session = Depends(get_session)) -> dict:
    """读取流程快照。"""
    try:
        snap = flow_store.get_flow(session, flow_id)
    except flow_store.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail="flow not found") from exc
    return _flow_view(snap)


@router.post("/generation-flows/{flow_id}/cancel")
def cancel_generation_flow(
    flow_id: str, body: AnswerInput, session: Session = Depends(get_session)
) -> dict:
    """协作取消：终态拒绝，active 流程转 cancelled。"""
    try:
        snap = flow_store.get_flow(session, flow_id)
    except flow_store.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail="flow not found") from exc

    try:
        snap = flow_store.transition_flow(
            session,
            flow_id=flow_id,
            expected_revision=body.revision,
            state=FlowState.CANCELLED,
        )
    except flow_store.StaleRevision as exc:
        raise _http_409(exc) from exc
    except flow_store.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail={"code": "invalid_transition", "message": str(exc)}) from exc

    # 取消传播：当前/排队中的阶段任务一并请求协作取消
    job = flow_store.latest_stage_job(session, flow_id)
    if job is not None and job.status in ("queued", "running"):
        queue_svc.request_cancel(session, job.id)

    return _flow_view(snap)


@router.post("/generation-flows/{flow_id}/answers", status_code=202)
def submit_answers(
    flow_id: str, body: AnswerInput, session: Session = Depends(get_session)
) -> dict:
    """回答必要缺口问题（幂等：重复 idempotency_key 不重复触发阶段）。"""
    try:
        snap = flow_store.get_flow(session, flow_id)
    except flow_store.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail="flow not found") from exc

    if snap.state != FlowState.AWAITING_INPUT:
        raise HTTPException(
            status_code=409,
            detail={"code": "not_awaiting_input", "message": f"当前状态 {snap.state}，无需回答"},
        )

    # 记录答案到检查点（T04 的 apply_answers 在阶段任务内消费）
    answers = dict(snap.checkpoint.get("answers") or {})
    answers[body.idempotency_key] = body.answers
    try:
        snap = flow_store.transition_flow(
            session,
            flow_id=flow_id,
            expected_revision=body.revision,
            state=FlowState.EXTRACTING,
            checkpoint={**snap.checkpoint, "answers": answers},
        )
    except flow_store.StaleRevision as exc:
        raise _http_409(exc) from exc
    except flow_store.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail={"code": "invalid_transition", "message": str(exc)}) from exc

    job = flow_store.run_stage_once(session, flow_id=flow_id, stage="extract")
    return _flow_view(snap, job_id=job.id)


@router.post("/generation-flows/{flow_id}/resume", status_code=202)
def resume_generation_flow(
    flow_id: str, body: AnswerInput, session: Session = Depends(get_session)
) -> dict:
    """恢复中断流程：INTERRUPTED → 只重排未完成阶段（不重放已完成的）。"""
    try:
        snap = flow_store.get_flow(session, flow_id)
    except flow_store.FlowNotFound as exc:
        raise HTTPException(status_code=404, detail="flow not found") from exc

    if snap.state != FlowState.INTERRUPTED:
        raise HTTPException(
            status_code=409,
            detail={"code": "not_interrupted", "message": f"当前状态 {snap.state}，无需恢复"},
        )

    # 恢复语义统一在 budget.resume_flow：按检查点定位前沿阶段（T11）
    try:
        snap = budget_svc.resume_flow(session, flow_id, body.revision)
    except flow_store.StaleRevision as exc:
        raise _http_409(exc) from exc
    except flow_store.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail={"code": "invalid_transition", "message": str(exc)}) from exc

    job = flow_store.latest_stage_job(session, flow_id)
    return _flow_view(snap, job_id=job.id if job is not None else None)


# ---------- v1.3 P1-2b：评测驱动修复（对话闭环入口） ----------


@router.post("/projects/{pid}/skill-versions/{version_id}/repair-eval", status_code=202)
def repair_from_eval(
    pid: str, version_id: str, session: Session = Depends(get_session)
) -> dict:
    """按评测失败证据创建修复流程（新版本，不改写冻结版本）。

    - 种子 = 父流程冻结候选（manifest 从版本行 files_json + 候选目录重建）
      + 上一次导出折算的失败 eval: 检查（证据驱动修复的输入）；
    - 新流程从 generate 起步（extract/plan 复用父流程产物，不重复消耗）；
    - 修复 → 重新静态验收 → 冻结新版本 → 重新排队对照评测（幂等键含
      新 version_id）——是否继续修复由用户依据新报告决策，循环入口
      即本端点；
    - 无失败评测证据（评测未跑/全过/未导出过）→ 409 nothing_to_repair。
    """
    row = _load_frozen_version(session, pid, version_id)
    if session.get(Project, pid) is None:
        raise HTTPException(status_code=404, detail="project not found")

    # 评测证据：复用导出折算逻辑（读流程套件 → 最近完成评测 → eval: 检查）
    eval_report = _eval_checks(session, pid, version_id)
    failed_eval = [c for c in eval_report.checks if c.status == CheckStatus.FAILED]
    if not failed_eval:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "nothing_to_repair",
                "message": "该版本没有失败的对照评测证据（未运行/未导出/已全部通过），无需修复",
            },
        )

    cfg = load_config()
    vdir = cfg.data_dir / "projects" / pid / "skills" / "versions" / version_id
    if not vdir.is_dir():
        raise HTTPException(status_code=404, detail="version directory missing")

    # 从冻结版本行重建 manifest（磁盘字节是唯一真相）；
    # binary 文件保持 resource_id 语义（修复层只改 text 文件）
    from app.generation.contracts import ArtifactFile, ArtifactKind, ArtifactManifest
    files = json.loads(row.files_json)
    artifacts: list[ArtifactFile] = []
    for f in files:
        # 缓存文件不进修复种子（旧版本可能冻结进过 pyc——已修，兼容历史）
        if "__pycache__" in f["path"] or f["path"].endswith(".pyc"):
            continue
        kind = ArtifactKind(f.get("kind", "text"))
        data = (vdir / f["path"]).read_bytes()
        if kind is ArtifactKind.BINARY:
            artifacts.append(ArtifactFile(
                path=f["path"], kind=kind, sha256=f.get("hash") or "",
                size=len(data), resource_id=f["path"],
            ))
        else:
            artifacts.append(ArtifactFile(
                path=f["path"], kind=kind,
                content=data.decode("utf-8"),
                sha256=f.get("hash") or "",
                size=len(data),
            ))
    manifest = ArtifactManifest(artifacts=artifacts)

    # 合同/方案/资源从父流程快照复用
    parent_flow_id = row.generation_flow_id or ""
    parent_row = session.get(GenerationFlow, parent_flow_id)
    snap = flow_store.get_flow(session, parent_flow_id)
    request = json.loads(parent_row.request_json) if parent_row else {}
    request = {**request, "repair_parent": {
        "flow_id": parent_flow_id, "version_id": version_id,
    }}

    new_flow = flow_store.create_flow(session, project_id=pid, request=request)
    # QUEUED → GENERATING：种子检查点（manifest + 失败评测证据）
    seeded = flow_store.transition_flow(
        session, new_flow.flow_id, new_flow.revision, FlowState.GENERATING,
        extra={"contract": snap.contract, "plan": snap.plan,
               "profile_snapshot": snap.profile_snapshot},
        checkpoint={"repair_from_eval": {
            "manifest": manifest.model_dump(),
            "report": eval_report.model_dump(),
            "parent_version_id": version_id,
            "parent_flow_id": parent_flow_id,
        }},
    )
    job = flow_store.run_stage_once(session, flow_id=new_flow.flow_id, stage="generate")
    return _flow_view(seeded, job_id=job.id)


# ---------- T16：打包导出 ----------


def _host_checks(
    session: Session, pid: str, version_id: str, receipt: DeliveryReceipt | None,
) -> CheckReport:
    """导出时刻的宿主验证（审计 A1）：按流程 target_platform 取适配器执行。

    - 验证是导出时刻行为，不回写冻结版本行（版本不可变）；
    - 适配器异常/无流程信息 → host 维度 not_run，绝不抛出阻断导出；
    - 证据来自宿主侧执行记录，API 注入 → FAILED（适配器内部判定）。
    """
    from app.models import GenerationFlow
    from app.generation.contracts import CheckReport, CheckResult, CheckStatus

    row = session.get(SkillVersion, version_id)
    flow = session.query(GenerationFlow).filter(
        GenerationFlow.id == (row.generation_flow_id if row else "")
    ).first()
    platform = ""
    cases: list[dict] = []
    if flow is not None:
        try:
            request = json.loads(flow.request_json) if flow.request_json else {}
            platform = str(request.get("target_platform") or "")
        except (json.JSONDecodeError, AttributeError):
            platform = ""
        # 触发用例：从检查点验收项推导（trigger 正/负例由适配器消费）；
        # case_id = acceptance_id:expect 保证唯一（防 CheckReport 重复 ID 拒绝）
        try:
            snap = flow_store.get_flow(session, flow.id)
            plan = snap.plan
            if plan is not None:
                cases = [
                    {"case_id": f"{acc.acceptance_id}:{exp}", "kind": "trigger",
                     "prompt": acc.description, "expect": exp}
                    for acc in plan.acceptances
                    for exp in ("hit", "miss")
                ]
        except Exception:  # noqa: BLE001 检查点异常不阻断导出，降级 not_run
            cases = []

    if not platform:
        return CheckReport(checks=[CheckResult(
            check_id="host:none:available", status=CheckStatus.NOT_RUN,
            evidence="blocked：流程无 target_platform，宿主验证未运行",
        )])

    adapter = get_adapter(platform)
    if adapter is None:
        return CheckReport(checks=[CheckResult(
            check_id=f"host:{platform}:available", status=CheckStatus.NOT_RUN,
            evidence=f"blocked：平台 {platform} 无适配器，宿主验证未运行",
        )])
    try:
        return adapter.validate_delivery(receipt, cases)
    except Exception as exc:  # noqa: BLE001 适配器故障降级 not_run，不阻断导出
        return CheckReport(checks=[CheckResult(
            check_id=f"host:{platform}:available", status=CheckStatus.NOT_RUN,
            evidence=f"blocked：宿主适配器异常（{type(exc).__name__}），验证未运行",
        )])


def _trigger_checks(session: Session, version_id: str) -> CheckReport:
    """导出时刻折算触发验证（v1.4 R1，与宿主验证/对照评测同模式）。

    - 生成链在交付时已排 trigger_run 任务（幂等键含 version_id）；
    - 这里按 kind=trigger 查最近已完成 EvalRun，转成 trigger: checks；
    - 异常/无运行 → not_run，绝不抛出阻断导出。
    """
    from app.generation.contracts import CheckReport, CheckResult, CheckStatus
    from app.workers.handlers.trigger_run import trigger_checks_report

    try:
        return trigger_checks_report(session, version_id)
    except Exception as exc:  # noqa: BLE001 折算故障降级 not_run，不阻断导出
        return CheckReport(checks=[CheckResult(
            check_id="trigger:summary", status=CheckStatus.NOT_RUN,
            evidence=f"blocked：触发验证折算异常（{type(exc).__name__}），未运行",
        )])


def _eval_checks(session: Session, pid: str, version_id: str) -> CheckReport:
    """导出时刻折算对照评测（v1.3 P1-2，与宿主验证同模式）。

    - 生成链在交付时已排 eval_run 任务（幂等键含 version_id）；
    - 这里按流程检查点的 suite_id + version_id 查最近已完成 EvalRun，
      转成 eval: checks（evaluate_candidate 分母纪律：失败/跳过计入）；
    - 无流程/无套件/尚无完成运行 → not_run，绝不扡出阻断导出。
    """
    from app.models import GenerationFlow
    from app.generation.contracts import CheckReport, CheckResult, CheckStatus

    row = session.get(SkillVersion, version_id)
    flow = session.query(GenerationFlow).filter(
        GenerationFlow.id == (row.generation_flow_id if row else "")
    ).first()
    if flow is None:
        return CheckReport(checks=[CheckResult(
            check_id="eval:summary", status=CheckStatus.NOT_RUN,
            evidence="blocked：无生成流程，对照评测未运行",
        )])
    try:
        snap = flow_store.get_flow(session, flow.id)
        suite_id = str(snap.checkpoint.get("eval_suite_id") or "")
    except Exception:  # noqa: BLE001 检查点异常不阻断导出，降级 not_run
        suite_id = ""
    if not suite_id:
        return CheckReport(checks=[CheckResult(
            check_id="eval:summary", status=CheckStatus.NOT_RUN,
            evidence="blocked：流程无评测套件（旧版本或评测未排队），对照评测未运行",
        )])
    try:
        return evaluate_candidate(session, flow.id, version_id, suite_id)
    except Exception as exc:  # noqa: BLE001 评测折算故障降级 not_run，不阻断导出
        return CheckReport(checks=[CheckResult(
            check_id="eval:summary", status=CheckStatus.NOT_RUN,
            evidence=f"blocked：对照评测折算异常（{type(exc).__name__}），未运行",
        )])


def _load_frozen_version(
    session: Session, pid: str, version_id: str
) -> SkillVersion:
    """取版本行并校验归属；缺生成元数据的版本（草稿创建）不可完整导出。"""
    row = session.get(SkillVersion, version_id)
    if row is None or row.project_id != pid:
        raise HTTPException(status_code=404, detail="version not found")
    if not (row.generation_flow_id and row.contract_json and row.report_json):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_exportable",
                "message": "该版本缺少生成元数据（合同/报告/流程），无法导出完整验证收据",
            },
        )
    return row


@router.post("/projects/{pid}/skill-versions/{version_id}/export")
def export_skill_version(
    pid: str, version_id: str, session: Session = Depends(get_session)
) -> dict:
    """打包完整交付 ZIP 并返回验证收据（T16）。

    - 版本/报告必须同 hash：与版本行 content_hash/contract/report 对账，
      不一致 → 409 version_hash_mismatch，拒绝导出；
    - 未验证候选可导出，但收据显示 candidate 而非验收通过。
    """
    row = _load_frozen_version(session, pid, version_id)
    cfg = load_config()
    vdir = cfg.data_dir / "projects" / pid / "skills" / "versions" / version_id
    if not vdir.is_dir():
        raise HTTPException(status_code=404, detail="version directory missing")

    contract = RequirementContract.model_validate_json(row.contract_json)
    checks = CheckReport.model_validate_json(row.report_json)

    # 审计 A1：导出时刻执行宿主验证，host: checks 并入报告（不回写冻结行）
    placeholder = DeliveryReceipt(
        flow_id=row.generation_flow_id or "pending-export",
        version_id=version_id,
        delivery_status=DeliveryStatus.CANDIDATE,
    )
    host_report = _host_checks(session, pid, version_id, placeholder)
    # v1.3 P1-2：导出时刻折算对照评测结果（eval: checks，同样不回写冻结行）
    eval_report = _eval_checks(session, pid, version_id)
    # v1.4 R1：导出时刻折算触发验证结果（trigger: checks，同模式不回写）
    trigger_report = _trigger_checks(session, version_id)
    checks = checks.model_copy(update={
        "checks": [c for c in checks.checks
                    if not (c.check_id.startswith("host:")
                            or c.check_id.startswith("eval:")
                            or c.check_id.startswith("trigger:"))]
        + eval_report.checks + trigger_report.checks + host_report.checks,
    })

    try:
        receipt = package_delivery(
            row.generation_flow_id, version_id, contract, checks, vdir,
            session=session,
        )
    except DeliveryError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "version_hash_mismatch", "message": str(exc)},
        ) from exc
    return receipt.model_dump()


@router.get("/projects/{pid}/skill-versions/{version_id}/export/download")
def download_skill_version_export(
    pid: str, version_id: str, session: Session = Depends(get_session)
) -> FileResponse:
    """下载已导出的交付 ZIP（须先调用 export 打包）。"""
    row = _load_frozen_version(session, pid, version_id)
    cfg = load_config()
    zip_path = (
        cfg.data_dir / "projects" / pid / "skills" / "exports" / f"{version_id}.zip"
    )
    if not zip_path.is_file():
        raise HTTPException(status_code=404, detail="export not found; run export first")
    return FileResponse(zip_path, media_type="application/zip",
                         filename=f"skill-{version_id}.zip")
