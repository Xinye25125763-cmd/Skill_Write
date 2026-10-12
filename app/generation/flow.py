"""生成流程存储与状态机（工程计划 §3 / T03）。

职责：
- create_flow：建 GenerationFlow 行（快照=FlowSnapshot 合同）；
- get_flow：读取并反序列化快照；
- transition_flow：CAS 推进（expected_revision 不匹配 → StaleRevision(409)），
  短事务提交状态与检查点；
- run_stage_once：同阶段唯一任务排队（stage_key 唯一 + 队列幂等双保险）；
- 状态机：UNKNOWN 状态与非法跳转拒绝（InvalidTransition）。

恢复语义（配合 T08）：阶段处理器从 checkpoint 恢复；API 写入一律带
expected_revision，冲突 409。
"""
from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.ulid import new_ulid
from app.generation.contracts import FlowBudget, FlowSnapshot, FlowState
from app.models import GenerationFlow, GenerationStageRun, Job
from app.services import queue

# 合法状态跳转（含暂停/恢复/取消/修复回路）
TRANSITIONS: dict[FlowState, frozenset[FlowState]] = {
    # GENERATING 直达：v1.3 P1-2b 评测驱动修复流程（repair-eval API 创建），
    # extract/plan 已由父流程完成，新流程直接从 generate（修复）起步
    FlowState.QUEUED: frozenset({FlowState.EXTRACTING, FlowState.GENERATING, FlowState.CANCELLED, FlowState.FAILED, FlowState.INTERRUPTED}),
    FlowState.EXTRACTING: frozenset({FlowState.AWAITING_INPUT, FlowState.PLANNING, FlowState.FAILED, FlowState.CANCELLED, FlowState.INTERRUPTED}),
    FlowState.AWAITING_INPUT: frozenset({FlowState.EXTRACTING, FlowState.PLANNING, FlowState.CANCELLED, FlowState.FAILED}),
    FlowState.PLANNING: frozenset({FlowState.GENERATING, FlowState.AWAITING_INPUT, FlowState.FAILED, FlowState.CANCELLED, FlowState.INTERRUPTED}),
    FlowState.GENERATING: frozenset({FlowState.VALIDATING, FlowState.FAILED, FlowState.CANCELLED, FlowState.INTERRUPTED}),
    FlowState.VALIDATING: frozenset({FlowState.EVALUATING, FlowState.REPAIRING, FlowState.GENERATING, FlowState.PACKAGING, FlowState.DELIVERED, FlowState.FAILED, FlowState.CANCELLED, FlowState.INTERRUPTED}),  # DELIVERED：静态验证通过即交付（评测接入后走 EVALUATING）
    FlowState.EVALUATING: frozenset({FlowState.REPAIRING, FlowState.HOST_CHECKING, FlowState.PACKAGING, FlowState.DELIVERED, FlowState.FAILED, FlowState.CANCELLED, FlowState.INTERRUPTED}),  # v1.3 P1-2：评测任务已排队即交付，导出时折算 eval 检查
    FlowState.REPAIRING: frozenset({FlowState.GENERATING, FlowState.VALIDATING, FlowState.FAILED, FlowState.CANCELLED, FlowState.INTERRUPTED}),
    FlowState.HOST_CHECKING: frozenset({FlowState.PACKAGING, FlowState.FAILED, FlowState.CANCELLED, FlowState.INTERRUPTED}),
    FlowState.PACKAGING: frozenset({FlowState.DELIVERED, FlowState.FAILED, FlowState.CANCELLED, FlowState.INTERRUPTED}),
    FlowState.DELIVERED: frozenset(),
    FlowState.FAILED: frozenset({FlowState.PLANNING}),   # 允许从失败重新规划
    FlowState.CANCELLED: frozenset(),
    FlowState.INTERRUPTED: frozenset({FlowState.QUEUED, FlowState.EXTRACTING, FlowState.PLANNING, FlowState.GENERATING, FlowState.VALIDATING}),  # 恢复（含各阶段中断后归位）
}


class StaleRevision(Exception):
    """CAS 失败：expected_revision 与当前不符（API 层映射 409）。"""

    def __init__(self, flow_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"409: 流程 {flow_id} revision 陈旧（expected={expected}, actual={actual}）"
        )
        self.flow_id = flow_id
        self.expected = expected
        self.actual = actual


class InvalidTransition(Exception):
    """非法状态跳转或未知状态。"""


class FlowNotFound(Exception):
    """流程不存在。"""


def create_flow(session: Session, project_id: str, request: dict[str, Any]) -> FlowSnapshot:
    """创建生成流程：初始快照 = queued + revision 1。"""
    flow_id = new_ulid()
    now = time.time()

    budget = FlowBudget.model_validate(request.get("budget") or {})
    snap = FlowSnapshot(
        flow_id=flow_id,
        project_id=project_id,
        state=FlowState.QUEUED,
        revision=1,
        budget=budget,
        checkpoint={},
    )
    row = GenerationFlow(
        id=flow_id,
        project_id=project_id,
        request_json=json.dumps(request, ensure_ascii=False),
        state=snap.state.value,
        revision=snap.revision,
        snapshot_json=snap.model_dump_json(),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    return snap


def get_flow(session: Session, flow_id: str) -> FlowSnapshot:
    """读取并反序列化流程快照。"""
    row = session.get(GenerationFlow, flow_id)
    if row is None:
        raise FlowNotFound(f"流程不存在: {flow_id}")
    return FlowSnapshot.model_validate_json(row.snapshot_json)


def transition_flow(
    session: Session,
    flow_id: str,
    expected_revision: int,
    state: FlowState | str,
    checkpoint: dict[str, Any] | None = None,
    *,
    extra: dict[str, Any] | None = None,
) -> FlowSnapshot:
    """CAS 推进流程状态。

    - expected_revision 不匹配 → StaleRevision（409 语义），数据零污染；
    - 未知状态字符串 → InvalidTransition；
    - 非法跳转 → InvalidTransition（终态只允许其声明的后继）；
    - extra 支持更新合同/规则/方案/报告等快照字段。
    """
    if isinstance(state, str):
        try:
            target = FlowState(state)
        except ValueError as exc:
            raise InvalidTransition(f"未知状态: {state!r}") from exc
    else:
        target = state

    row = session.get(GenerationFlow, flow_id)
    if row is None:
        raise FlowNotFound(f"流程不存在: {flow_id}")

    snap = FlowSnapshot.model_validate_json(row.snapshot_json)
    if snap.revision != expected_revision:
        session.rollback()
        raise StaleRevision(flow_id, expected_revision, snap.revision)

    current = FlowState(snap.state.value) if isinstance(snap.state, str) else snap.state
    if current is not target and target not in TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"非法状态跳转: {current.value} → {target.value}")

    snap = snap.model_copy(update={
        "state": target,
        "revision": expected_revision + 1,
        **({"checkpoint": checkpoint} if checkpoint is not None else {}),
        **(extra or {}),
    })
    row.state = target.value
    row.revision = snap.revision
    row.snapshot_json = snap.model_dump_json()
    row.updated_at = time.time()
    session.commit()
    return snap


def run_stage_once(session: Session, flow_id: str, stage: str) -> Job:
    """排队一个阶段任务；同阶段重复请求返回既有任务（不新建）。

    幂等双保险：
    1. generation_stage_runs.stage_key = f"{flow_id}:{stage}" 唯一；
    2. 队列 (type, idempotency_key=stage_key) 去重。
    """
    stage_key = f"{flow_id}:{stage}"

    existing = (
        session.query(GenerationStageRun)
        .filter_by(stage_key=stage_key)
        .one_or_none()
    )
    if existing is not None:
        job = session.get(Job, existing.job_id)
        if job is not None and job.status in ("queued", "running"):
            return job
        # 旧任务已终结：允许重排（检查点恢复语义），更新登记行的 job_id
        job = _enqueue_stage(session, flow_id, stage, stage_key)
        existing.job_id = job.id
        session.commit()
        return job

    job = _enqueue_stage(session, flow_id, stage, stage_key)
    session.add(GenerationStageRun(
        id=new_ulid(),
        flow_id=flow_id,
        stage=stage,
        job_id=job.id,
        stage_key=stage_key,
        created_at=time.time(),
    ))
    session.commit()
    return job


def latest_stage_job(session: Session, flow_id: str) -> Job | None:
    """流程当前（最近登记的）阶段任务；无登记返回 None。"""
    rows = (
        session.query(GenerationStageRun)
        .filter_by(flow_id=flow_id)
        .order_by(GenerationStageRun.created_at.desc())
        .all()
    )
    for r in rows:
        job = session.get(Job, r.job_id)
        if job is not None:
            return job
    return None


def _enqueue_stage(session: Session, flow_id: str, stage: str, stage_key: str) -> Job:
    # 重排前提：旧的同键任务已终结（上方分支保证）——释放其幂等键，
    # 让 queue.enqueue 的去重只针对活跃任务，否则会返回旧终结任务、
    # 永远不会被 Worker 领取（流程卡死）。
    session.query(Job).filter(
        Job.type == "generation_stage",
        Job.idempotency_key == stage_key,
        Job.status.in_(("succeeded", "failed", "cancelled", "interrupted")),
    ).update({"idempotency_key": None}, synchronize_session=False)
    payload = {"flow_id": flow_id, "stage": stage}
    job = queue.enqueue(
        session,
        job_type="generation_stage",
        payload=payload,
        idempotency_key=stage_key,
    )
    return job
