"""v1.5 M1：套件预算字段消费验收（此前为死字段）。

场景：内建套件每例带 budget.max_tokens_per_case / max_tokens_total，
eval_run 此前从不读取——执行时用执行器默认 200k。M1 让字段生效：
1. 逐例限制：per_case 传入 execute_prompt（API 显式传参仍最高优先）；
2. 整体封顶：累计消耗达 suite max_tokens_total 后余例 skipped；
3. meta_json.budget 记录声明上限、实际累计、封顶是否触发。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.llm import ModelResponse, Usage
from app.models import EvalCaseResult, EvalRun, Suite
from app.workers.worker import Worker

AUTH = {"x-session-token": "test-token"}


class GenScriptedProvider:
    """生成链脚本模型（contract → plan → SKILL.md）。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        if not self.script:
            return ModelResponse(content="{}", usage=Usage(10, 5),
                                 finish_reason="stop")
        item = self.script.pop(0)
        return ModelResponse(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
            usage=Usage(100, 200), finish_reason="stop",
        )


class GreedyAgentProvider:
    """执行侧假模型：每次回复都带工具调用，直到预算耗尽。

    usage 每次 (5000, 500) → per-case 8k 限制下第 2 轮即撞墙。
    记录收到的 messages 长度供断言。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.msg_counts: list[int] = []

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=True, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        from app.llm import ToolCall
        self.calls += 1
        self.msg_counts.append(len(messages))
        # 第 3 轮起不再调工具（若预算还活着）
        if self.calls % 3 == 0:
            return ModelResponse(content="已完成 REQ-1：行动项整理",
                                 usage=Usage(5_000, 500), finish_reason="stop")
        return ModelResponse(
            content="继续", usage=Usage(5_000, 500), finish_reason="tool_calls",
            tool_calls=[ToolCall(id=f"c{self.calls}", name="write_file",
                                 arguments={"path": "out.md", "content": "x"})],
        )


def _contract() -> dict:
    return {"requirements": [{
        "requirement_id": "REQ-1", "content": "整理纪要为行动项", "necessary": True,
        "source_type": "user_message", "source_ref": "m1", "confirmation": "confirmed",
    }], "conflicts": [], "open_questions": []}


def _plan() -> dict:
    return {"plan_id": "p1",
            "files": [{"path": "SKILL.md", "kind": "text", "purpose": "主文档",
                        "requirement_ids": ["REQ-1"]}],
            "acceptances": [{"acceptance_id": "A1", "kind": "static",
                             "description": "SKILL.md 存在", "requirement_ids": ["REQ-1"]}]}


def _good_skill() -> str:
    return ("---\nname: action-extractor\n"
            "description: 把会议纪要整理为行动项表格。\n---\n\n"
            "# 行动项整理\n\n整理会议纪要为行动项。输出需包含 REQ-1。\n")


def _run_chain(client: TestClient, monkeypatch: pytest.MonkeyPatch,
               agent) -> str:
    """跑完整生成链（含自动排队评测），返回 version_id。"""
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod

    gen = GenScriptedProvider([_contract(), _plan(),
                               {"path": "SKILL.md", "kind": "text",
                                "complete": True, "content": _good_skill()}])
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: agent)

    pid = client.post("/api/projects", json={"name": "m1-budget"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    Worker().run_pending()
    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered", snap.get("error")
    return snap["checkpoint"]["receipt"]["version_id"]


def test_builtin_suite_budget_is_consumed(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主路径：内建套件带的 per-case 80k 限制传入执行器。

    断言落库的 AgentRun.budget_json.limit_tokens == 80000（M1 前
    是执行器默认 200_000——字段死了）。
    """
    agent = GreedyAgentProvider()
    vid = _run_chain(client, monkeypatch, agent)

    from app.models import AgentRun, EvalRun
    with Session(create_db_engine(db_path(load_config()))) as s:
        ev = (s.query(EvalRun)
              .filter(EvalRun.version_id == vid, EvalRun.kind != "trigger")
              .order_by(EvalRun.created_at.desc()).first())
        assert ev is not None and ev.status == "finished"

        # 内建套件常量已校准（evaluation.py）
        suite = s.get(Suite, ev.suite_id)
        cases = json.loads(suite.cases_json)
        assert cases[0]["budget"]["max_tokens_per_case"] == 80_000
        assert cases[0]["budget"]["max_tokens_total"] == 200_000

        # 每次执行的预算墙 = 80_000（不是执行器默认 200_000）
        runs = (s.query(AgentRun)
                .filter(AgentRun.id.in_(
                    [r.agent_run_id for r in
                     s.query(EvalCaseResult)
                     .filter(EvalCaseResult.eval_run_id == ev.id).all()
                     if r.agent_run_id])).all())
        assert runs
        for r in runs:
            limit = json.loads(r.budget_json or "{}").get("limit_tokens")
            assert limit == 80_000, f"per-case 限制应传入执行器，实际 {limit}"

        # meta_json.budget 段：套件声明 + 实际累计 + 未触发封顶
        meta = json.loads(ev.meta_json)
        assert meta["budget"]["suite_total_cap"] == 200_000
        assert meta["budget"]["used_tokens"] > 0
        assert meta["budget"]["cap_hit"] is False


def test_suite_total_cap_skips_remaining_cases(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整体封顶：把套件 per-case 与 total 调小 → 首例撞墙耗尽 → 余例 skipped。

    用 GreedyAgentProvider（每次 5500 token）+ per-case 6_000 +
    total 12_000：首例两组共花 ~12k → 后续用例不再执行。
    """
    agent = GreedyAgentProvider()
    vid = _run_chain(client, monkeypatch, agent)

    from app.models import AgentRun, EvalRun
    with Session(create_db_engine(db_path(load_config()))) as s:
        ev = (s.query(EvalRun)
              .filter(EvalRun.version_id == vid, EvalRun.kind != "trigger")
              .order_by(EvalRun.created_at.desc()).first())
        assert ev is not None

        # 手工改套件预算并重跑评测（走 API 入口验证同一消费路径）
        suite = s.get(Suite, ev.suite_id)
        cases = json.loads(suite.cases_json)
        for c in cases:
            c["budget"] = {"max_tokens_per_case": 6_000,
                           "max_tokens_total": 12_000}
            c["repeats"] = 2
        # 加第二个用例（首例耗尽后它应被 skip）
        if len(cases) == 1:
            cases.append({
                "id": "case:REQ-1:b", "prompt": "另一个整理任务 REQ-1",
                "assertions": [{"type": "text_contains", "contains": "REQ-1"}],
                "repeats": 1, "budget": cases[0]["budget"],
            })
        suite.cases_json = json.dumps(cases, ensure_ascii=False)
        s.commit()

        eval_project_id = ev.project_id
        eval_suite_id = ev.suite_id

    # Run through the public API so the field contract is exercised end to end.
    response = client.post(
        f"/api/projects/{eval_project_id}/eval-runs",
        json={"suite_id": eval_suite_id, "version_id": vid,
              "max_tokens_per_run": 6_000, "max_tokens_total": 12_000},
        headers=AUTH,
    )
    assert response.status_code == 202, response.text
    eval_job_id = response.json()["job_id"]
    Worker().run_pending()

    with Session(create_db_engine(db_path(load_config()))) as s:
        from app.models import Job
        finished_job = s.get(Job, eval_job_id)
        assert finished_job is not None and finished_job.result_json
        eval_id = json.loads(finished_job.result_json)["id"]
        ev2 = s.get(EvalRun, eval_id)
        rows = (s.query(EvalCaseResult)
                .filter(EvalCaseResult.eval_run_id == ev2.id)
                .order_by(EvalCaseResult.created_at).all())
        # 首例两组真实执行（budget_exceeded）；后续用例 skipped
        executed = [r for r in rows if r.exec_status == "executed"]
        skipped = [r for r in rows if r.exec_status == "skipped"]
        assert executed, "首例应真实执行"
        assert skipped, "封顶后余例应 skipped"
        assert any("封顶" in (r.score_error or "") for r in skipped)
        # 执行侧预算墙生效
        for r in executed:
            ar = s.query(AgentRun).filter(AgentRun.id == r.agent_run_id).first()
            if ar is not None and r.exec_status == "executed":
                limit = json.loads(ar.budget_json or "{}").get("limit_tokens")
                assert 100 <= limit <= 6_000
        limits = [json.loads(s.query(AgentRun).filter(AgentRun.id == r.agent_run_id).first().budget_json or "{}").get("limit_tokens")
                  for r in rows if r.agent_run_id]
        assert 6_000 in limits
        assert min(limits) < 6_000  # remaining suite allowance constrains later run/repeat

        meta = json.loads(ev2.meta_json)
        assert meta["budget"]["suite_total_cap"] == 12_000
        assert meta["budget"]["cap_hit"] is True
        assert meta["budget"]["cap_accounted_tokens"] >= 12_000


def test_budget_priority_explicit_case_suite_and_legacy_alias() -> None:
    from app.workers.handlers.eval_run import _resolve_budget_limits

    cases = [
        {"budget": {"max_tokens_per_case": 1_000, "max_tokens_total": 5_000}},
        {"budget": {"max_tokens_per_case": 2_000}},
    ]
    per_run, suite_cap, limits = _resolve_budget_limits(cases, {})
    assert per_run is None
    assert suite_cap == 5_000
    assert limits == [1_000, 2_000]

    per_run, suite_cap, limits = _resolve_budget_limits(
        cases, {"max_tokens_per_run": 3_000, "max_tokens_total": 50_000},
    )
    assert per_run == 3_000
    assert suite_cap == 50_000
    assert limits == [3_000, 3_000]

    per_run, suite_cap, limits = _resolve_budget_limits(
        cases, {"max_tokens_per_run": 4_000, "max_tokens_total": 2_000},
    )
    assert per_run == 4_000
    assert suite_cap == 2_000
    assert limits == [4_000, 4_000]

    # The early suite_max_tokens_total spelling stays as a compatibility alias;
    # canonical max_tokens_total wins when both are present.
    _, suite_cap, _ = _resolve_budget_limits(
        cases, {"max_tokens_total": 30_000, "suite_max_tokens_total": 50_000},
    )
    assert suite_cap == 30_000
    _, suite_cap, _ = _resolve_budget_limits(
        cases, {"suite_max_tokens_total": 50_000},
    )
    assert suite_cap == 50_000


def test_case_budget_partial_fields_inherit_suite_per_run_default() -> None:
    from app.workers.handlers.eval_run import _resolve_budget_limits

    cases = [
        {"budget": {"max_tokens_per_case": 8_000, "max_tokens_total": 40_000}},
        {"budget": {"max_tokens_total": 10_000}},
    ]
    _, _, limits = _resolve_budget_limits(cases, {})
    assert limits == [8_000, 8_000]


def test_eval_result_view_exposes_budget_scope_and_actual_tokens(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = GreedyAgentProvider()
    vid = _run_chain(client, monkeypatch, agent)

    from app.models import EvalRun
    from app.workers.handlers.eval_run import eval_run_view
    with Session(create_db_engine(db_path(load_config()))) as s:
        ev = (s.query(EvalRun)
              .filter(EvalRun.version_id == vid, EvalRun.kind != "trigger")
              .order_by(EvalRun.created_at.desc()).first())
        assert ev is not None
        view = eval_run_view(s, ev)
        assert view["budget"]["suite_total_cap"] == 200_000
        assert view["budget"]["used_tokens"] > 0
        assert view["budget"]["cap_hit"] is False
        assert "judge" in view["budget"]["suite_cap_scope"]
        assert view["cost"]["candidate_tokens"] > 0
        assert view["cost"]["baseline_tokens"] > 0
        from app.generation.delivery import _evaluation_usage_receipt
        receipt_usage = _evaluation_usage_receipt(s, vid)
        contrast = next(item for item in receipt_usage if item["kind"] == "contrast")
        assert contrast["budget"]["suite_total_cap"] == 200_000
        assert contrast["cost"]["candidate_tokens"] > 0
        assert contrast["cost"]["baseline_tokens"] > 0


@pytest.mark.parametrize("total_cap", [1_000, 50_000])
def test_eval_api_preserves_explicit_suite_total_override(
    migrated_db: Any, client: TestClient, project: dict, total_cap: int,
) -> None:
    from app.models import Job

    suite_response = client.post(
        f"/api/projects/{project['id']}/suites",
        json={"name": "带预算套件", "cases": [{
            "id": "budget-case", "prompt": "测试预算覆盖",
            "budget": {"max_tokens_per_case": 10_000, "max_tokens_total": 10_000},
        }]},
        headers=AUTH,
    )
    assert suite_response.status_code == 201, suite_response.text
    response = client.post(
        f"/api/projects/{project['id']}/eval-runs",
        json={"suite_id": suite_response.json()["id"], "version_id": "01AAAAAAAAAAAAAAAAAAAAAAAA",
              "max_tokens_per_run": 20_000, "max_tokens_total": total_cap},
        headers=AUTH,
    )
    assert response.status_code == 202, response.text
    with Session(create_db_engine(db_path(load_config()))) as session:
        job = session.get(Job, response.json()["job_id"])
        assert job is not None
        payload = json.loads(job.payload_json)
        assert payload["max_tokens_per_run"] == 20_000
        assert payload["max_tokens_total"] == total_cap
        assert "suite_max_tokens_total" not in payload
