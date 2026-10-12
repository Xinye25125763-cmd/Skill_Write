"""全流程预算、停止与可恢复执行（工程计划 §5 T11）。

记账模型（checkpoint["budget_ledger"]，按 stage_id + attempt_id 记录）：
    {"stage@job-id:attempt": {"stage_id": ..., "reserved": n, "actual": {...} | None}}

- 阶段前 reserve_stage_budget：预留即占用（used_tokens 计入预留额，
  预算是跑道不是事后报表——不知道的消耗不能当免费）；
- 阶段后 record_stage_usage：按实际 usage 覆盖该阶段记账（同 stage_id
  重复记录覆盖不累加 → 不重记）；usage 未知（None）时按预留保底计费
  并记至少 1 次模型调用；
- 跨生成/评测/修复累计：同一 attempt 幂等重记，不同 attempt 通过账本键分别保存并累计；
- 超额显式报告：used_tokens 如实累计、不静默钳制、不抛"免费"异常——
  预算是显式报告线，不保证账单硬上限（J 样本纪律）。

写入纪律：
- 账本写入不推进 revision（revision 只表达状态变化）；
- 走库层直读 + 条件更新（WHERE revision=:rev）重试合并——绝不基于
  ORM 身份映射缓存的陈旧快照回写（会把并发取消/状态推进抹掉）；
- 写后 expire_all，防同会话后续读取继续拿到陈旧缓存。

resume_flow：INTERRUPTED → 只恢复未完成阶段（按检查点定位前沿），
已完成阶段不重放、不重扣（I 样本纪律）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.generation import flow as flow_store
from app.generation.contracts import FlowBudget, FlowSnapshot, FlowState

# 各阶段默认预留 tokens（调用方可显式传 amount 覆盖）
RESERVE_TOKENS: dict[str, int] = {
    "extract": 2000,
    "plan": 4000,
    "generate": 12000,
    "eval": 4000,
    "eval-blind": 4000,  # v1.4 R3：盲评评审（每例一次调用）
    "trigger": 2000,  # v1.4 R1：触发验证（生成+判定+有界改写）
    "validate": 1000,
    "repair": 8000,
}
_DEFAULT_RESERVE = 4000


class BudgetLedgerStale(Exception):
    """账本写入连续撞上并发状态变更——放弃写入，不污染账本。"""


def _fresh_snapshot(session: Session, flow_id: str) -> tuple[FlowSnapshot, int]:
    """从库层直读快照（绕过 ORM 身份映射）。

    Worker 会话可能缓存了陈旧的 GenerationFlow 对象（取消/状态推进由
    另一会话提交）；session.get 命中缓存不重查，若用缓存快照回写会把
    并发状态（如 CANCELLED）抹掉。账本读写必须走库层真相。
    """
    row = session.execute(
        text("SELECT revision, snapshot_json FROM generation_flows WHERE id=:id"),
        {"id": flow_id},
    ).first()
    if row is None:
        raise flow_store.FlowNotFound(f"流程不存在: {flow_id}")
    return FlowSnapshot.model_validate_json(row.snapshot_json), int(row.revision)


def _ledger_of(snap: FlowSnapshot) -> dict[str, dict[str, Any]]:
    return dict(snap.checkpoint.get("budget_ledger") or {})


def _recompute(base: FlowBudget, ledger: dict[str, dict[str, Any]]) -> FlowBudget:
    """按 ledger 重算 used_*：未记实际的按预留计，已记实际的按实际计。"""
    used_tokens = 0
    used_calls = 0
    estimated_tokens = 0
    unknown_calls = 0
    for entry in ledger.values():
        actual = entry.get("actual")
        if actual is None:
            used_tokens += int(entry.get("reserved") or 0)
            estimated_tokens += int(entry.get("reserved") or 0)
            # A merely reserved stage has uncertain token cost, not a measured
            # model call. The reservation remains visibly estimated.
        else:
            known = int(actual.get("prompt_tokens", 0)) + int(
                actual.get("completion_tokens", 0)
            )
            estimated = int(actual.get("unknown_usage_estimated_tokens", 0))
            used_tokens += known + estimated
            estimated_tokens += estimated
            used_calls += int(actual.get("model_calls", 1))
            unknown_calls += int(actual.get("usage_missing_calls") or 0)
    return base.model_copy(update={
        "used_tokens": used_tokens,
        "used_model_calls": used_calls,
        "estimated_tokens": estimated_tokens,
        "unknown_usage_calls": unknown_calls,
    })


def _apply_ledger(
    session: Session, flow_id: str, mutate: Callable[[FlowSnapshot], FlowSnapshot | None]
) -> FlowBudget:
    """把台账变更合并到最新快照并落盘（不推进 revision）。

    条件更新 WHERE revision=:rev：若期间有并发状态推进（取消/CAS），
    重读最新快照重试——台账合并在当前真实状态之上，绝不覆盖并发结果。
    mutate 返回 None 表示无需写入（幂等命中）。
    """
    last_budget: FlowBudget | None = None
    for _ in range(3):
        snap, rev = _fresh_snapshot(session, flow_id)
        updated = mutate(snap)
        if updated is None:
            return snap.budget
        last_budget = updated.budget
        result = session.execute(
            text(
                "UPDATE generation_flows SET snapshot_json=:snap, updated_at=:ts "
                "WHERE id=:id AND revision=:rev"
            ),
            {"snap": updated.model_dump_json(), "ts": time.time(),
             "id": flow_id, "rev": rev},
        )
        if result.rowcount == 1:
            session.commit()
            session.expire_all()  # 防同会话身份映射继续读陈旧快照
            return last_budget
        session.rollback()
    raise BudgetLedgerStale(f"账本写入连续撞上并发变更: {flow_id}")


def _ledger_key(stage_id: str, attempt_id: str | None) -> str:
    return f"{stage_id}@{attempt_id}" if attempt_id else stage_id


def reserve_stage_budget(
    session: Session, flow_id: str, stage: str, amount: int | None = None,
    *, attempt_id: str | None = None,
) -> FlowBudget:
    """阶段前预留；同一阶段尝试幂等，不同 attempt 各自累计。"""
    ledger_key = _ledger_key(stage, attempt_id)
    reserve = (
        int(amount) if amount is not None else RESERVE_TOKENS.get(stage, _DEFAULT_RESERVE)
    )

    def mutate(snap: FlowSnapshot) -> FlowSnapshot | None:
        ledger = _ledger_of(snap)
        entry = dict(ledger.get(ledger_key) or {})
        if entry.get("actual") is not None or entry.get("reserved"):
            return None  # 幂等：同一 attempt 已预留或已记实际
        entry["stage_id"] = stage
        if attempt_id is not None:
            entry["attempt_id"] = attempt_id
        entry["reserved"] = reserve
        ledger[ledger_key] = entry
        budget = _recompute(snap.budget, ledger)
        return snap.model_copy(update={
            "budget": budget,
            "checkpoint": {**snap.checkpoint, "budget_ledger": ledger},
        })

    return _apply_ledger(session, flow_id, mutate)


def record_stage_usage(
    session: Session, flow_id: str, stage_id: str, usage: dict[str, Any] | None,
    *, attempt_id: str | None = None,
) -> FlowBudget:
    """记录一次阶段尝试；重记同 attempt 幂等，retry 用新 attempt_id 累计。"""
    ledger_key = _ledger_key(stage_id, attempt_id)

    def mutate(snap: FlowSnapshot) -> FlowSnapshot:
        ledger = _ledger_of(snap)
        entry = dict(ledger.get(ledger_key) or {})
        entry.setdefault("stage_id", stage_id)
        if attempt_id is not None:
            entry["attempt_id"] = attempt_id
        reserved = int(entry.get("reserved") or _DEFAULT_RESERVE)
        if usage is None:
            u = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "unknown_usage_estimated_tokens": reserved,
                "model_calls": 1,
                "usage_missing_calls": 1,
                "usage_unknown": True,
                "estimated": True,
            }
        elif usage.get("usage_unknown"):
            u = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "unknown_usage_estimated_tokens": reserved,
                "model_calls": int(usage.get("model_calls") or 1),
                "usage_missing_calls": int(usage.get("model_calls") or 1),
                "usage_unknown": True,
                "estimated": True,
            }
        else:
            u = dict(usage)
            u.setdefault("model_calls", 1)
            if int(u.get("usage_missing_calls") or 0) > 0:
                known = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
                estimate = int(u.get("unknown_usage_estimated_tokens") or 0)
                if estimate == 0:
                    u["unknown_usage_estimated_tokens"] = max(0, reserved - known)
                u["estimated"] = True
            if u.get("unknown_usage_estimated_tokens"):
                u["estimated"] = True
        entry["actual"] = u
        ledger[ledger_key] = entry
        budget = _recompute(snap.budget, ledger)
        return snap.model_copy(update={
            "budget": budget,
            "checkpoint": {**snap.checkpoint, "budget_ledger": ledger},
        })

    return _apply_ledger(session, flow_id, mutate)


def usage_from_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """阶段元数据 → 记账 usage；有调用但无用量 → None（未知不免费）。"""
    if not isinstance(meta, dict):
        return None
    calls = int(meta.get("model_calls") or 0)
    usage = meta.get("usage") or meta.get("usage_observed") or {}
    if calls > 0 and not usage:
        return {"model_calls": calls, "usage_unknown": True}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
        "model_calls": calls,
        "usage_missing_calls": int(meta.get("usage_missing_calls") or 0),
        "unknown_usage_estimated_tokens": int(meta.get("unknown_usage_estimated_tokens") or 0),
    }


_STAGE_STATE = {
    "extract": FlowState.EXTRACTING,
    "plan": FlowState.PLANNING,
    "generate": FlowState.GENERATING,
}


def resume_flow(session: Session, flow_id: str, revision: int) -> FlowSnapshot:
    """恢复中断流程：只重排未完成阶段（检查点定位前沿），不重放已完成。

    前沿规则：有方案 → generate；有合同 → plan；否则 extract。
    """
    snap = flow_store.get_flow(session, flow_id)
    if snap.state != FlowState.INTERRUPTED:
        raise flow_store.InvalidTransition(
            f"流程不在 interrupted 状态（当前 {snap.state.value}），无需恢复"
        )
    if snap.revision != revision:
        session.rollback()
        raise flow_store.StaleRevision(flow_id, revision, snap.revision)

    if snap.plan is not None:
        stage = "generate"
    elif snap.contract is not None:
        stage = "plan"
    else:
        stage = "extract"

    snap = flow_store.transition_flow(
        session, flow_id, revision, _STAGE_STATE[stage]
    )
    flow_store.run_stage_once(session, flow_id=flow_id, stage=stage)
    return snap
