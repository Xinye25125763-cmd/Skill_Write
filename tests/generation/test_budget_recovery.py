"""T11：全流程预算、停止和可恢复执行。

本项验收（工程计划 §5 T11，样本 J/I）：
1. J：跨生成/评测/修复累计；同 attempt 重复记录不重扣，不同 attempt 保留历史并累计；
2. I：取消/宕机后只恢复未完成阶段（已完成阶段不重跑、不重扣）；
3. usage 未知不当作免费（按预留估算，并保留已知 model_calls 与 missing usage 次数）；
4. 不重放已完成有副作用步骤（resume 只重排前沿阶段）；
5. 超额显式报告（used_tokens 如实累计，不静默钳制；流程不假装免费）。
"""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import create_engine

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.llm import Capabilities, ModelResponse, Usage
from app.models import Base, Project

AUTH = {"x-session-token": "test-token"}

GOOD_SKILL_MD = "---\nname: weekly-report\ndescription: 周报\n---\n# 周报\n"


class ScriptedProvider:
    capabilities = Capabilities(tools=False, streaming=False, json_mode=True, max_context_tokens=100_000)

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[list] = []

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN202
        self.calls.append(list(messages))
        if not self.script:
            return ModelResponse(content="{}", usage=Usage(10, 5), finish_reason="stop")
        item = self.script.pop(0)
        return ModelResponse(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
            usage=Usage(100, 200), finish_reason="stop",
        )


def _script() -> list[dict]:
    return [
        {"requirements": [{
            "requirement_id": "REQ-001", "content": "按模板输出周报",
            "necessary": True, "source_type": "user_message", "source_ref": "m",
            "confirmation": "confirmed",
        }], "conflicts": [], "open_questions": []},
        {"plan_id": "plan-1",
         "files": [{"path": "SKILL.md", "kind": "text", "purpose": "主",
                    "requirement_ids": ["REQ-001"]}],
         "acceptances": [{"acceptance_id": "ACC-001", "kind": "static",
                          "description": "头信息", "requirement_ids": ["REQ-001"]}],
         "tool_notes": [], "blocking_gaps": []},
        {"path": "SKILL.md", "kind": "text", "complete": True, "content": GOOD_SKILL_MD},
    ]


def _flow_in_db(migrated_db: Path) -> str:
    from app.generation.flow import create_flow
    with Session(create_engine(f"sqlite:///{migrated_db}")) as s:
        pid = "01AAAAAAAAAAAAAAAAAAAAAAAA"
        if s.get(Project, pid) is None:
            s.add(Project(id=pid, name="p"))
            s.commit()
        snap = create_flow(s, project_id=pid, request={"request_text": "x"})
        return snap.flow_id


def test_usage_tracking_provider_caps_each_output_and_stops_later_calls() -> None:
    from app.llm.usage import ModelTokenBudgetExceeded, UsageTrackingProvider

    class Provider:
        capabilities = Capabilities(tools=False, streaming=False, json_mode=True,
                                    max_context_tokens=10_000)

        def __init__(self) -> None:
            self.max_tokens: list[int | None] = []

        def complete(self, messages, **kwargs):  # noqa: ANN001, ANN201
            self.max_tokens.append(kwargs.get("max_tokens"))
            return ModelResponse(content="ok", usage=Usage(70, 40))

    raw = Provider()
    provider = UsageTrackingProvider(raw, max_tokens_total=100)
    provider.complete([])
    assert raw.max_tokens == [100]
    assert provider.used_tokens == 110  # prompt tokens can overrun an in-flight call
    assert provider.meta()["overrun_tokens"] == 10
    with pytest.raises(ModelTokenBudgetExceeded):
        provider.complete([])
    assert raw.max_tokens == [100], "额度耗尽后不应向底层 provider 再发请求"


# ---------- 验收 1+3：累计 / 不重记 / 未知不免费（单元级） ----------


def test_total_budget_across_generation_eval_repair_and_crash(migrated_db: Path) -> None:
    from app.generation.budget import (
        record_stage_usage,
        reserve_stage_budget,
    )
    from app.generation.flow import get_flow

    flow_id = _flow_in_db(migrated_db)

    # 阶段前预留
    b = reserve_stage_budget(s := _sess(migrated_db), flow_id, "extract", amount=2000)
    assert b.used_tokens == 2000  # 预留即占用（不是免费跑道）

    # 阶段后按实际 usage 记录（覆盖预留）
    b = record_stage_usage(s, flow_id, "extract",
                           {"prompt_tokens": 100, "completion_tokens": 200, "model_calls": 1})
    assert b.used_tokens == 300
    assert b.used_model_calls == 1

    # 跨阶段累计：plan + generate（J：跨生成累计）
    reserve_stage_budget(s, flow_id, "plan", amount=4000)
    record_stage_usage(s, flow_id, "plan",
                       {"prompt_tokens": 100, "completion_tokens": 200, "model_calls": 1})
    reserve_stage_budget(s, flow_id, "generate", amount=12000)
    record_stage_usage(s, flow_id, "generate",
                       {"prompt_tokens": 100, "completion_tokens": 200, "model_calls": 1})
    b = record_stage_usage(s, flow_id, "repair-1",
                           {"prompt_tokens": 50, "completion_tokens": 50, "model_calls": 1})
    assert b.used_tokens == 1000  # 300×3 + 100（修复轮也累计）
    assert b.used_model_calls == 4

    # 不重记：同 stage_id 再记一次 → 覆盖，不累加
    b = record_stage_usage(s, flow_id, "extract",
                           {"prompt_tokens": 100, "completion_tokens": 200, "model_calls": 1})
    assert b.used_tokens == 1000, "同阶段重复记录不得双扣"

    # 验收 3：usage 未知不当作免费
    reserve_stage_budget(s, flow_id, "eval", amount=3000)
    b = record_stage_usage(s, flow_id, "eval", None)
    assert b.used_model_calls == 5, "未知 usage 至少记 1 次调用"
    assert b.used_tokens >= 1000 + 1000, "未知 usage 按保底计费（不低于已记 + 预留）"

    # 验收 5：超额显式报告——used_tokens 如实累计，不钳制
    snap = get_flow(s, flow_id)
    assert snap.budget.total_tokens == 200_000
    small = _flow_in_db(migrated_db)
    with Session(create_engine(f"sqlite:///{migrated_db}")) as s2:
        from app.generation.flow import get_flow as gf
        from app.generation.budget import reserve_stage_budget as rs
        gf(s2, small).budget.model_dump()
    with Session(create_engine(f"sqlite:///{migrated_db}")) as s3:
        from app.generation import flow as fs
        snap_small = fs.get_flow(s3, small)
        # 压低总预算到 100
        fs.transition_flow(s3, small, snap_small.revision, snap_small.state,
                           extra={"budget": snap_small.budget.model_copy(
                               update={"total_tokens": 100})})
    b = rs(s3, small, "extract", amount=2000)
    assert b.used_tokens == 2000 > 100, "超额如实报告，不静默钳制"
    b = record_stage_usage(s3, small, "extract", {"prompt_tokens": 100, "completion_tokens": 200, "model_calls": 1})
    assert b.used_tokens == 300 > 100


def _sess(migrated_db: Path) -> Session:
    return Session(create_engine(f"sqlite:///{migrated_db}"))


# ---------- 验收 2+4：宕机恢复只跑未完成阶段，不重放不重扣（端到端） ----------


def test_crash_resume_only_unfinished_stage_no_replay(
    client: TestClient, project: dict, migrated_db: Path, monkeypatch
) -> None:
    from app.generation import flow as flow_store
    from app.generation.budget import resume_flow
    from app.generation.contracts import FlowState
    from app.workers.worker import Worker

    pid = project["id"]
    provider = ScriptedProvider(_script())
    import app.workers.handlers.generation_stage as gs
    monkeypatch.setattr(gs, "provider_factory", lambda: provider)

    r = client.post(
        f"/api/projects/{pid}/generation-flows",
        json={"request_text": "周报助手"},
        headers=AUTH,
    )
    assert r.status_code == 202, r.text
    flow_id = r.json()["flow_id"]

    worker = Worker()
    worker.run_once()  # 只跑 extract（plan 任务已排队但未执行）

    with Session(create_db_engine(db_path(load_config()))) as s:
        snap = flow_store.get_flow(s, flow_id)
        assert snap.state == FlowState.PLANNING
        ledger = snap.checkpoint.get("budget_ledger") or {}
        extract_key = next(key for key in ledger if key.startswith("extract@"))
        assert ledger[extract_key].get("actual"), "extract 已完成应有实际 usage 记录"
        extract_usage_before = ledger[extract_key]["actual"]
        # 宕机：转 interrupted
        flow_store.transition_flow(s, flow_id, snap.revision, FlowState.INTERRUPTED)

    calls_before_crash = len(provider.calls)

    # 恢复：只重排未完成阶段（plan；extract 不重放）
    with Session(create_db_engine(db_path(load_config()))) as s:
        snap = flow_store.get_flow(s, flow_id)
        resume_flow(s, flow_id, snap.revision)

    worker.run_pending()
    view = client.get(f"/api/generation-flows/{flow_id}", headers=AUTH).json()
    assert view["state"] == "delivered", view

    # 不重放：总调用数 = extract(1) + plan(1) + generate(1)，无重跑
    assert len(provider.calls) == calls_before_crash + 2, "恢复后只补跑未完成阶段"

    # 不重扣：ledger 里 extract 的 usage 与宕机前一致；预算无重复记账
    with Session(create_db_engine(db_path(load_config()))) as s:
        snap = flow_store.get_flow(s, flow_id)
        ledger = snap.checkpoint.get("budget_ledger") or {}
        extract_key = next(key for key in ledger if key.startswith("extract@"))
        assert ledger[extract_key]["actual"] == extract_usage_before
        # 3 个阶段各 300 tokens：extract+plan+generate
        assert snap.budget.used_tokens == 900, snap.budget
        assert snap.budget.used_model_calls == 3


def test_budget_ledger_accumulates_distinct_attempts_but_is_idempotent(
    migrated_db: Path,
) -> None:
    from app.generation.budget import record_stage_usage, reserve_stage_budget
    from app.generation.flow import get_flow

    flow_id = _flow_in_db(migrated_db)
    s = _sess(migrated_db)
    reserve_stage_budget(s, flow_id, "generate", amount=1000, attempt_id="job-a:1")
    first = record_stage_usage(
        s, flow_id, "generate", {"prompt_tokens": 10, "completion_tokens": 20, "model_calls": 2},
        attempt_id="job-a:1",
    )
    assert first.used_tokens == 30
    # A persistence retry for the same attempt replaces its entry.
    repeated = record_stage_usage(
        s, flow_id, "generate", {"prompt_tokens": 15, "completion_tokens": 25, "model_calls": 2},
        attempt_id="job-a:1",
    )
    assert repeated.used_tokens == 40
    # A worker retry is a new actual provider attempt and must be accumulated.
    reserve_stage_budget(s, flow_id, "generate", amount=1000, attempt_id="job-a:2")
    second = record_stage_usage(
        s, flow_id, "generate", {"prompt_tokens": 11, "completion_tokens": 19, "model_calls": 1},
        attempt_id="job-a:2",
    )
    assert second.used_tokens == 70
    assert second.used_model_calls == 3
    snap = get_flow(s, flow_id)
    ledger = snap.checkpoint["budget_ledger"]
    assert "generate@job-a:1" in ledger
    assert "generate@job-a:2" in ledger


def test_usage_from_meta_preserves_unknown_call_count(migrated_db: Path) -> None:
    from app.generation.budget import (
        record_stage_usage, reserve_stage_budget, usage_from_meta,
    )
    from app.generation.flow import get_flow

    unknown = usage_from_meta({"model_calls": 3, "usage": None})
    assert unknown == {"model_calls": 3, "usage_unknown": True}
    flow_id = _flow_in_db(migrated_db)
    session = _sess(migrated_db)
    reserve_stage_budget(session, flow_id, "plan", amount=4_000, attempt_id="job-b:1")
    budget = record_stage_usage(
        session, flow_id, "plan", unknown, attempt_id="job-b:1",
    )
    assert budget.used_tokens == 4_000  # reservation estimate; unknown is not free
    assert budget.used_model_calls == 3
    ledger = get_flow(session, flow_id).checkpoint["budget_ledger"]
    entry = ledger["plan@job-b:1"]["actual"]
    assert entry["estimated"] is True
    assert entry["prompt_tokens"] == 0
    assert entry["unknown_usage_estimated_tokens"] == 4_000
    assert entry["usage_unknown"] is True
    assert entry["model_calls"] == 3


def test_partial_usage_estimates_only_the_unknown_remainder(migrated_db: Path) -> None:
    from app.generation.budget import (
        record_stage_usage, reserve_stage_budget, usage_from_meta,
    )
    from app.generation.flow import get_flow

    flow_id = _flow_in_db(migrated_db)
    session = _sess(migrated_db)
    reserve_stage_budget(session, flow_id, "extract", amount=1_000, attempt_id="job-c:1")
    meta = {
        "model_calls": 3,
        "usage": None,
        "usage_observed": {"prompt_tokens": 100, "completion_tokens": 200},
        "usage_calls": 2,
        "usage_missing_calls": 1,
        "unknown_usage_estimated_tokens": 50,
    }
    budget = record_stage_usage(
        session, flow_id, "extract", usage_from_meta(meta), attempt_id="job-c:1",
    )
    assert budget.used_tokens == 350
    assert budget.estimated_tokens == 50
    assert budget.unknown_usage_calls == 1
    assert budget.used_model_calls == 3
    entry = get_flow(session, flow_id).checkpoint["budget_ledger"]["extract@job-c:1"]["actual"]
    assert entry["prompt_tokens"] + entry["completion_tokens"] == 300
    assert entry["unknown_usage_estimated_tokens"] == 50
