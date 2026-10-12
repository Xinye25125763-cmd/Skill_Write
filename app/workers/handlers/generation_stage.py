"""generation_stage 处理器（工程计划 §5 T08）：阶段生成接入现有队列。

阶段语义（每个阶段独立 Job，短事务提交状态与下一任务）：
- extract  : 提取需求合同；有未决必要缺口 → awaiting_input（不占 Worker，
             无下一任务）。有新回答时确定性修订合同（apply_answers），
             不再消耗模型调用。
- plan     : 生成工程方案 → 排 generate 任务。
- generate : 逐文件生成 → 暂存 → 静态验证 → 需求覆盖；
             验证失败 → REPAIRING（证据驱动有界修复，v1.3 P1-2 接入）；
             修复后回 generate 重验收（重新暂存/验证/冻结）；
             静态验证通过 → EVALUATING → 排 eval_run 队列任务，
             评测（对照证据）由独立 Worker 执行，本阶段即交付，
             导出时折算 eval 检查（与宿主验证同模式）。

围栏纪律：
- 终态围栏：流程已交付/取消/失败 → 直接跳过（skipped_terminal）；
- 取消围栏：发布前最后检查 ctx.is_cancelled()（调用中取消不发布候选）；
- CAS 围栏：每个阶段首次状态写入用领取时捕获的 revision——迟到 Worker
  撞上 StaleRevision / InvalidTransition → outcome=superseded，
  检查点零污染；
- 异常先 rollback 再把流程标记 failed，结构化错误进 job.error。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.generation import budget as budget_svc
from app.generation import flow as flow_store
from app.generation.artifacts import manifest_hash, publish_candidate, stage_artifacts
from app.generation.contracts import (
    ArtifactManifest,
    CheckReport,
    CheckResult,
    CheckStatus,
    DeliveryReceipt,
    DeliveryStatus,
    FlowSnapshot,
    FlowState,
)
from app.generation.coverage import evaluate_coverage, summarize_coverage
from app.generation.evaluation import build_eval_suite
from app.generation.generator import GenerationError, generate_candidate
from app.generation.planning import PlanBuildError, build_skill_plan
from app.generation.policy import load_profile, profile_hash
from app.generation.prompts import PROMPT_VERSION
from app.generation.repair import repair_candidate
from app.generation.requirements import (
    ContractExtractionError,
    apply_answers,
    extract_contract,
    unanswered_gaps,
)
from app.generation.validation import validate_candidate
from app.generation.versioning import FreezeError, freeze_candidate
from app.llm import ProviderNotConfigured, provider_from_env
from app.llm.usage import ModelTokenBudgetExceeded, UsageTrackingProvider
from app.models import GenerationFlow
from app.services import queue
from app.workers.handlers import HandlerContext, JobCancelled, payload_of, register

# 测试可替换的提供商工厂（生产读环境变量）
provider_factory = provider_from_env

# v1.3 P1-2：验证失败后的有界修复上限（含首轮，即最多 1+max 次验证循环）
MAX_REPAIR_ROUNDS_IN_FLOW = 2

_ROOT = Path(__file__).resolve().parents[3]  # 仓库根（profile 相对路径锚点）

_TERMINAL = (FlowState.DELIVERED, FlowState.CANCELLED, FlowState.FAILED)


def _profile(cfg) -> Any:  # noqa: ANN001
    """加载工程规则集；相对路径锚定到仓库根。"""
    path = Path(cfg.generation.profile_path)
    if not path.is_absolute():
        path = _ROOT / path
    return load_profile(path)


def _load_resources(cfg, resource_ids: list[str]) -> dict[str, bytes]:
    """按 ID 装载已上传资源字节（声明缺失的资源由 generator 层拒绝）。"""
    root = Path(cfg.data_dir) / "resources"
    out: dict[str, bytes] = {}
    for rid in resource_ids or []:
        meta_file = root / rid / "meta.json"
        if not meta_file.is_file():
            continue
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        f = root / rid / meta.get("path", "")
        if f.is_file():
            out[rid] = f.read_bytes()
    return out


def _mark_failed(session, flow_id: str, message: str) -> None:  # noqa: ANN001
    """异常 rollback 后把流程标记 failed（不掩盖原始异常）。"""
    session.rollback()
    try:
        snap = flow_store.get_flow(session, flow_id)
        if snap.state not in _TERMINAL:
            flow_store.transition_flow(
                session, flow_id, snap.revision, FlowState.FAILED,
                extra={"error": message},
            )
    except Exception:  # noqa: BLE001
        session.rollback()


@register("generation_stage")
def handle(session, cfg, job, ctx: HandlerContext) -> dict:  # noqa: ANN001
    payload = payload_of(job)
    flow_id = str(payload["flow_id"])
    stage = str(payload["stage"])

    row = session.get(GenerationFlow, flow_id)
    if row is None:
        raise flow_store.FlowNotFound(f"流程不存在: {flow_id}")
    snap = flow_store.get_flow(session, flow_id)

    # 终态围栏
    if snap.state in _TERMINAL:
        return {"outcome": "skipped_terminal", "flow_id": flow_id,
                "state": snap.state.value}

    token_cap = (snap.budget.max_tokens_total
                 if snap.budget.max_tokens_total is not None
                 else snap.budget.total_tokens)
    if snap.budget.used_tokens >= token_cap:
        message = f"全流程 token 预算已耗尽（{snap.budget.used_tokens}/{token_cap}），后续阶段未执行"
        _mark_failed(session, flow_id, message)
        return {"outcome": "budget_exceeded", "flow_id": flow_id,
                "state": FlowState.FAILED.value, "error": message}

    # CAS 围栏基准：领取（读取快照）时刻的 revision
    start_revision = snap.revision

    try:
        attempt_id = f"{job.id}:{int(job.attempt or 1)}"
        return _run_stage(session, cfg, ctx, row, snap, stage, start_revision, attempt_id)
    except (flow_store.StaleRevision, flow_store.InvalidTransition) as exc:
        # 迟到 Worker：流程已被更新推进，本阶段结果作废，不覆盖检查点
        session.rollback()
        return {"outcome": "superseded", "flow_id": flow_id, "reason": str(exc)}
    except (ContractExtractionError, PlanBuildError, GenerationError) as exc:
        _mark_failed(session, flow_id, str(exc))
        raise
    except FreezeError as exc:
        _mark_failed(session, flow_id, f"冻结失败: {exc}")
        raise
    except ProviderNotConfigured as exc:
        _mark_failed(session, flow_id, f"模型提供商未配置: {exc}")
        raise
    except ModelTokenBudgetExceeded as exc:
        message = str(exc)
        _mark_failed(session, flow_id, message)
        return {"outcome": "budget_exceeded", "flow_id": flow_id,
                "state": FlowState.FAILED.value, "error": message}


def _budgeted_provider(snap: FlowSnapshot, stage: str, attempt_id: str) -> UsageTrackingProvider:
    """Limit all calls in this stage by the remaining whole-flow token budget."""
    cap = (snap.budget.max_tokens_total
           if snap.budget.max_tokens_total is not None else snap.budget.total_tokens)
    key = f"{stage}@{attempt_id}"
    entry = (snap.checkpoint.get("budget_ledger") or {}).get(key, {})
    # Only subtract an unsettled reservation. Once actual usage was recorded,
    # retrying the same job attempt must not reclaim that already-spent budget.
    reserved = int(entry.get("reserved") or 0) if entry.get("actual") is None else 0
    already_used = max(0, snap.budget.used_tokens - reserved)
    allowance = max(0, cap - already_used)
    return UsageTrackingProvider(
        provider_factory(), max_tokens_total=allowance,
    )


def _run_stage(session, cfg, ctx, row, snap: FlowSnapshot, stage: str,
               start_revision: int, attempt_id: str) -> dict:
    if stage == "extract":
        return _stage_extract(session, cfg, ctx, row, snap, start_revision, attempt_id)
    if stage == "plan":
        return _stage_plan(session, cfg, ctx, snap, start_revision, attempt_id)
    if stage == "generate":
        return _stage_generate(session, cfg, ctx, row, snap, start_revision, attempt_id)
    raise ValueError(f"未知阶段: {stage!r}")


def _repair_rounds(snap: FlowSnapshot) -> int:
    """检查点里已消耗的修复轮次（不存在 → 0）。"""
    return int(snap.checkpoint.get("repair_rounds") or 0)


def _repair_exhausted_note(rounds: int) -> CheckResult:
    """修复预算耗尽的如实备注（进报告，不伪装通过）。"""
    return CheckResult(
        check_id="repair:exhausted", status=CheckStatus.NOT_RUN,
        evidence=(
            f"有界修复 {rounds} 轮后验证仍失败：按未验证候选交付，"
            "失败证据见各项检查（不假装修好）"
        ),
    )


def _run_repair_round(
    session, cfg, snap: FlowSnapshot, contract, profile,
    manifest, report, round_no: int, attempt_id: str,
):
    """v1.3 P1-2：单轮证据驱动修复（budget 记账 repair-{n}，幂等）。"""
    flow_id = snap.flow_id
    stage_id = f"repair-{round_no}"
    repair_attempt = f"{attempt_id}:repair-{round_no}"
    budget_svc.reserve_stage_budget(session, flow_id, stage_id, attempt_id=repair_attempt)

    snap = flow_store.get_flow(session, flow_id)
    provider = _budgeted_provider(snap, stage_id, repair_attempt)
    try:
        repaired, meta = repair_candidate(
            provider, contract, profile, manifest, report,
            return_meta=True, max_rounds=1,
        )
    except Exception:
        if provider.model_calls:
            budget_svc.record_stage_usage(
                session, flow_id, stage_id, budget_svc.usage_from_meta(provider.meta()),
                attempt_id=repair_attempt,
            )
        raise
    meta = {**meta, **provider.meta()}
    budget_svc.record_stage_usage(
        session, flow_id, stage_id, budget_svc.usage_from_meta(meta),
        attempt_id=repair_attempt,
    )
    return repaired


def _stage_extract(session, cfg, ctx, row, snap: FlowSnapshot,
                   start_revision: int, attempt_id: str) -> dict:
    flow_id = snap.flow_id
    # 入口 CAS 围栏：仅当状态需要推进时写入（QUEUED/AWAITING_INPUT → EXTRACTING）。
    # 状态已到位时跳过，避免无意义的 revision 递增（API 与阶段写入不相互过期）。
    if snap.state != FlowState.EXTRACTING:
        snap = flow_store.transition_flow(
            session, flow_id, start_revision, FlowState.EXTRACTING
        )
    ctx.event("extract", "需求提取")

    # T11：阶段前预留（预留即占用）。账本写入不推进 revision，
    # 但会让本内存快照的检查点变陈旧——记账后必须重新读取。
    budget_svc.reserve_stage_budget(session, flow_id, "extract", attempt_id=attempt_id)
    snap = flow_store.get_flow(session, flow_id)

    request: dict[str, Any] = json.loads(row.request_json)
    raw_answers: dict[str, Any] = snap.checkpoint.get("answers") or {}
    # 扁平化：API 按幂等键存储，多个回答合并为 {requirement_id: {...}}
    answers: dict[str, Any] = {}
    for batch in raw_answers.values():
        if isinstance(batch, dict):
            answers.update(batch)
    stored = snap.contract

    if stored is not None and answers:
        # 有既有合同 + 新回答 → 确定性修订（apply_answers），不消耗模型调用
        contract = apply_answers(stored, answers)
        meta = {"prompt_version": PROMPT_VERSION, "model_calls": 0,
                "revised_by": "answers"}
    else:
        provider = _budgeted_provider(snap, "extract", attempt_id)
        if answers:
            request = {**request, "answers": answers}
        try:
            contract, meta = extract_contract(
                provider, request, _profile(cfg), return_meta=True
            )
        except Exception:
            if provider.model_calls:
                budget_svc.record_stage_usage(
                    session, flow_id, "extract", budget_svc.usage_from_meta(provider.meta()),
                    attempt_id=attempt_id,
                )
            raise
        meta = {**meta, **provider.meta()}

    # T11：阶段后按实际 usage 记账（同 attempt 可重试，跨 attempt 累加）
    budget_svc.record_stage_usage(
        session, flow_id, "extract", budget_svc.usage_from_meta(meta),
        attempt_id=attempt_id,
    )
    snap = flow_store.get_flow(session, flow_id)

    pending = unanswered_gaps(contract)
    if pending:
        snap = flow_store.transition_flow(
            session, flow_id, snap.revision, FlowState.AWAITING_INPUT,
            extra={
                "contract": contract,
                "profile_snapshot": _profile(cfg),
                "pending_questions": pending,
            },
        )
        return {"outcome": "awaiting_input", "flow_id": flow_id,
                "state": snap.state.value, "pending_questions": pending}

    next_job = flow_store.run_stage_once(session, flow_id, "plan")
    snap = flow_store.transition_flow(
        session, flow_id, snap.revision, FlowState.PLANNING,
        extra={
            "contract": contract,
            "profile_snapshot": _profile(cfg),
            "pending_questions": [],
        },
        checkpoint={**snap.checkpoint, "extract_meta": meta},
    )
    return {"outcome": "planning", "flow_id": flow_id, "state": snap.state.value,
            "job_id": next_job.id}


def _stage_plan(session, cfg, ctx, snap: FlowSnapshot,
                start_revision: int, attempt_id: str) -> dict:
    flow_id = snap.flow_id
    if snap.state != FlowState.PLANNING:
        snap = flow_store.transition_flow(
            session, flow_id, start_revision, FlowState.PLANNING
        )
    ctx.event("plan", "工程方案生成")

    # T11：阶段前预留
    budget_svc.reserve_stage_budget(session, flow_id, "plan", attempt_id=attempt_id)
    snap = flow_store.get_flow(session, flow_id)

    contract = snap.contract
    if contract is None:
        raise PlanBuildError("快照缺少需求合同（流程损坏）")
    profile = snap.profile_snapshot or _profile(cfg)
    provider = _budgeted_provider(snap, "plan", attempt_id)
    try:
        plan, meta = build_skill_plan(provider, contract, profile, return_meta=True)
    except Exception:
        if provider.model_calls:
            budget_svc.record_stage_usage(
                session, flow_id, "plan", budget_svc.usage_from_meta(provider.meta()),
                attempt_id=attempt_id,
            )
        raise
    meta = {**meta, **provider.meta()}

    # T11：阶段后记账
    budget_svc.record_stage_usage(
        session, flow_id, "plan", budget_svc.usage_from_meta(meta),
        attempt_id=attempt_id,
    )
    snap = flow_store.get_flow(session, flow_id)

    next_job = flow_store.run_stage_once(session, flow_id, "generate")
    snap = flow_store.transition_flow(
        session, flow_id, snap.revision, FlowState.GENERATING,
        extra={"plan": plan},
        checkpoint={**snap.checkpoint, "plan_meta": meta},
    )
    return {"outcome": "generating", "flow_id": flow_id, "state": snap.state.value,
            "job_id": next_job.id}


def _stage_generate(session, cfg, ctx, row, snap: FlowSnapshot,
                    start_revision: int, attempt_id: str) -> dict:
    flow_id = snap.flow_id
    if snap.state != FlowState.GENERATING:
        snap = flow_store.transition_flow(
            session, flow_id, start_revision, FlowState.GENERATING
        )
    ctx.event("generate", "技能文件生成")

    # T11：阶段前预留
    budget_svc.reserve_stage_budget(session, flow_id, "generate", attempt_id=attempt_id)
    snap = flow_store.get_flow(session, flow_id)

    contract = snap.contract
    plan = snap.plan
    if contract is None or plan is None:
        raise GenerationError("快照缺少合同或方案（流程损坏）")
    profile = snap.profile_snapshot or _profile(cfg)

    request: dict[str, Any] = json.loads(row.request_json)
    resources = _load_resources(cfg, request.get("resource_ids") or [])

    # v1.3 P1-2b：评测驱动修复种子（repair-eval API 创建的新流程）——
    # 以父流程冻结候选起步，不重新生成（评测失败不是生成失败的重复）；
    # 评测失败证据在首次验证后并入报告，交由下方既有修复回路消费
    eval_seed = snap.checkpoint.get("repair_from_eval")
    repaired_manifest = snap.checkpoint.get("repair_manifest")
    if eval_seed is not None and _repair_rounds(snap) == 0:
        manifest = ArtifactManifest.model_validate(eval_seed["manifest"])
        meta = {"model_calls": 0, "usage": None, "via": "repair_from_eval"}
    elif _repair_rounds(snap) > 0 and repaired_manifest is not None:
        manifest = ArtifactManifest.model_validate(repaired_manifest)
        meta = {"model_calls": 0, "usage": None, "via": "repair"}
    else:
        provider = _budgeted_provider(snap, "generate", attempt_id)
        try:
            manifest, meta = generate_candidate(
                provider, contract, plan, profile, resources, return_meta=True
            )
        except Exception:
            if provider.model_calls:
                budget_svc.record_stage_usage(
                    session, flow_id, "generate", budget_svc.usage_from_meta(provider.meta()),
                    attempt_id=attempt_id,
                )
            raise
        meta = {**meta, **provider.meta()}
        # T11：阶段后记账（同一 job attempt 幂等，worker retry 累加）
        budget_svc.record_stage_usage(
            session, flow_id, "generate", budget_svc.usage_from_meta(meta),
            attempt_id=attempt_id,
        )
    snap = flow_store.get_flow(session, flow_id)

    # 取消围栏：发布暂存前的最后一道闸——调用中取消不发布候选
    if ctx.is_cancelled():
        raise JobCancelled()

    root = Path(cfg.data_dir) / "generation" / snap.project_id
    staged = stage_artifacts(flow_id, manifest, resources, root)

    # T09：独立静态验证（磁盘字节是唯一真相）；失败 → 证据驱动有界修复回路
    # （v1.3 P1-2）：修复消耗模型调用（budget 记账 repair-N），
    # 修好后由新一轮 generate 任务重验收（重新暂存/验证/冻结）
    profile = snap.profile_snapshot or _profile(cfg)
    snap = flow_store.transition_flow(
        session, flow_id, snap.revision, FlowState.VALIDATING,
        extra={"candidate_manifest": manifest},
    )
    report = validate_candidate(manifest, contract, profile, staged)

    # v1.3 P1-2b：评测驱动修复——静态验证通过但评测失败时，把评测失败
    # 证据（eval: 检查）并入报告，进入下方既有证据驱动修复回路；
    # 修复后重新静态验收，新版本会重新排队对照评测（闭环交给导出/再触发）
    if eval_seed is not None and _repair_rounds(snap) == 0:
        seed_report = CheckReport.model_validate(eval_seed["report"])
        failed_eval = [c for c in seed_report.checks
                       if c.status == CheckStatus.FAILED]
        if failed_eval:
            report = report.model_copy(update={
                "checks": report.checks + failed_eval,
            })

    repair_rounds_done = _repair_rounds(snap)
    # 就地修复循环（不重排 generate 任务：同阶段 handler 内 run_stage_once
    # 会把当前 running 任务当作"已排队"返回，造成流程卡死）
    while report.has_failed and repair_rounds_done < MAX_REPAIR_ROUNDS_IN_FLOW:
        repair_rounds_done += 1
        snap = flow_store.transition_flow(
            session, flow_id, snap.revision, FlowState.REPAIRING,
            checkpoint={**snap.checkpoint, "repair_rounds": repair_rounds_done},
        )
        manifest = _run_repair_round(
            session, cfg, snap, contract, profile, manifest, report,
            repair_rounds_done, attempt_id,
        )
        snap = flow_store.get_flow(session, flow_id)
        snap = flow_store.transition_flow(
            session, flow_id, snap.revision, FlowState.VALIDATING,
            extra={"candidate_manifest": manifest},
            checkpoint={**snap.checkpoint,
                        "repair_manifest": manifest.model_dump()},
        )
        staged = stage_artifacts(flow_id, manifest, resources, root)
        if ctx.is_cancelled():
            raise JobCancelled()
        report = validate_candidate(manifest, contract, profile, staged)

    if report.has_failed:
        # 修复预算耗尽仍失败：如实带证据交付 candidate，不伪装通过
        report = report.model_copy(update={"checks": report.checks + [
            _repair_exhausted_note(repair_rounds_done),
        ]})

    # 取消围栏：验证耗时后再次确认（发布前最后检查）
    if ctx.is_cancelled():
        raise JobCancelled()


    candidate_dir = publish_candidate(staged, manifest_hash(manifest), root)

    phash = profile_hash(profile)
    version_id = freeze_candidate(
        session, flow_id, manifest, root / "candidate" / flow_id,
        contract=contract, profile_hash=phash, report=report,
        cfg=cfg, project_id=snap.project_id,
    )

    # T12：需求覆盖证明——未运行的验收不得算 verified；收据如实携带覆盖报告
    coverage = evaluate_coverage(contract, plan, manifest, report)
    coverage_status = summarize_coverage(coverage)

    # v1.3 P1-2：静态验证后 → EVALUATING，排对照评测（独立 eval_run 队列任务，
    # 幂等键含 version_id）；本阶段即交付不阻塞评测，导出时折算 eval 检查
    # （与宿主验证同模式：不回写冻结版本行）
    suite_id = build_eval_suite(session, contract, plan,
                                project_id=snap.project_id)
    eval_job = queue.enqueue(
        session, "eval_run",
        {"project_id": snap.project_id, "suite_id": suite_id,
         "version_id": version_id},
        idempotency_key=f"eval:{flow_id}:{version_id}",
    )

    # v1.4 R1：交付同时排触发验证（第六维 trigger，幂等键含 version_id）；
    # 触发不合格不阻塞导出，导出时刻折算 trigger: checks（不回写冻结行）
    queue.enqueue(
        session, "trigger_run",
        {"project_id": snap.project_id, "version_id": version_id},
        idempotency_key=f"trigger:{flow_id}:{version_id}",
    )

    receipt = DeliveryReceipt(
        flow_id=flow_id,
        version_id=version_id,
        # Evaluation and host checks are queued asynchronously; this checkpoint
        # is a generated candidate, not a completed acceptance receipt.
        delivery_status=DeliveryStatus.CANDIDATE,
        static_status=(CheckStatus.FAILED if report.has_failed else CheckStatus.PASSED),
        coverage_status=coverage_status,
        notes=[
            "需求对照评测与触发/宿主验证已排队；导出时会合并实际结果，"
            "全部必要维度通过前此版本保持候选状态",
        ] + ([
            f"coverage={coverage_status.value}: 执行/人工类验收尚未接入（T13–T15），"
            "静态验证通过的候选仍标注未验证维度"
        ] if coverage_status != CheckStatus.PASSED else []),
        requirement_coverage=coverage.checks,
    )

    snap = flow_store.transition_flow(
        session, flow_id, snap.revision, FlowState.EVALUATING,
        checkpoint={**snap.checkpoint, "generate_meta": meta,
                    "staging_path": str(staged),
                    "eval_suite_id": suite_id, "eval_job_id": eval_job.id,
                    "receipt": receipt.model_dump()},
    )
    snap = flow_store.transition_flow(
        session, flow_id, snap.revision, FlowState.DELIVERED,
        extra={"latest_report": report},
    )
    return {"outcome": "delivered", "flow_id": flow_id, "state": snap.state.value,
            "version_id": version_id,
            "eval_job_id": eval_job.id, "suite_id": suite_id,
            "files": [a.path for a in manifest.artifacts]}
