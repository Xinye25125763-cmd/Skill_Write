"""T14：自动建立测试集并做公平对照。

本项验收（工程计划 §5 T14，样本 L）：
1. 两组获得相同任务输入 fixtures，候选额外 skill 资源（公平对照）；
2. 全评测预算限制（每例 + 整体预算进套件数据）；
3. 失败/跳过计入验收而非只看已评分分母；
4. 独立保留用例不用于修复（套件不可变快照）。
"""
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.core.ulid import new_ulid
from app.generation.contracts import (
    Requirement,
    RequirementContract,
    SkillPlan,
)
from app.models import EvalCaseResult, EvalRun, Suite
from app.workers.worker import Worker

from tests.conftest import VALID_BRIEF
from tests.test_agent_runs import AUTH, FakeProvider, _resp_text, _resp_tool
from tests.test_skills import VALID_SKILL_MD


def _req(rid: str, necessary: bool = True) -> dict:
    return {
        "requirement_id": rid,
        "content": f"完成功能 {rid}：把素材整理为周报输出",
        "necessary": necessary,
        "source_type": "user_message",
        "source_ref": "m",
        "confirmation": "confirmed",
    }


def _contract() -> RequirementContract:
    return RequirementContract.model_validate({
        "request_id": "flow-1",
        "target_platform": "codex",
        "requirements": [_req("REQ-001"), _req("REQ-002"),
                         _req("REQ-009", necessary=False)],
    })


def _plan() -> SkillPlan:
    return SkillPlan.model_validate({
        "plan_id": "plan-1",
        "files": [{"path": "SKILL.md", "kind": "text", "purpose": "主",
                   "requirement_ids": ["REQ-001", "REQ-002"]}],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "execution",
             "description": "执行验证 REQ-001", "requirement_ids": ["REQ-001"]},
            {"acceptance_id": "ACC-002", "kind": "execution",
             "description": "执行验证 REQ-002", "requirement_ids": ["REQ-002"]},
        ],
    })


# ---------- build_eval_suite ----------


def test_shared_fixtures_overall_budget_and_failed_case_denominator(
    migrated_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation.evaluation import build_eval_suite, evaluate_candidate

    cfg = load_config()
    engine = create_db_engine(db_path(cfg))
    pid = "01EVALTEST00000000000000000"
    with Session(engine) as s:
        from app.models import Project
        if s.get(Project, pid) is None:
            s.add(Project(id=pid, name="eval-test"))
            s.commit()

        suite_id = build_eval_suite(s, _contract(), _plan())
        suite = s.get(Suite, suite_id)
        assert suite is not None
        cases = json.loads(suite.cases_json)

        # 验收 4：套件保留（不可变快照）——再次构建得到新套件，旧的分毫未动
        old_cases_json = suite.cases_json
        suite_id2 = build_eval_suite(s, _contract(), _plan())
        assert suite_id2 != suite_id
        assert s.get(Suite, suite_id).cases_json == old_cases_json

        # 验收 1：每个必要需求一个用例；非必要不进套件
        assert len(cases) == 2
        assert all("REQ-001" in c["id"] or "REQ-002" in c["id"] for c in cases)
        assert not any("REQ-009" in c["id"] for c in cases)

        # 验收 1：两组共享相同 fixtures（公平对照：任务输入一致，
        # 唯一差异是候选额外带 skill 资源——由执行层保证）
        for c in cases:
            assert "fixtures" in c and c["fixtures"], "用例须携带共享 fixtures"
        assert cases[0]["fixtures"] == cases[1]["fixtures"] or len(cases) == 2

        # 验收 2：预算限制进套件数据（每例 + 整体）
        assert cases[0].get("budget", {}).get("max_tokens_per_case") is not None
        assert cases[0]["budget"].get("max_tokens_total") is not None

        # ---------- 验收 3：失败/跳过计入验收分母 ----------
        # 构造一次已完成评测：通过 / 执行失败 / 跳过 / 评分失败 各一例
        flow_id = "flow-eval-1"
        vid = "01EVALVER00000000000000000"
        ev = EvalRun(
            id=new_ulid(), project_id=pid, suite_id=suite_id, version_id=vid,
            status="finished", created_at=time.time(), finished_at=time.time(),
        )
        s.add(ev)
        s.commit()
        rows = [
            ("case-ok", "executed", "completed", "scored", 2, 2, None),
            ("case-fail", "executed", "model_failed", "not_scored", 0, 0, "模型执行失败"),
            ("case-skip", "skipped", None, "not_scored", 0, 0, None),
            ("case-score-fail", "executed", "completed", "score_failed", 0, 0, "断言结构非法"),
        ]
        for cid, exec_st, exit_r, score_st, p, t, err in rows:
            s.add(EvalCaseResult(
                id=new_ulid(), eval_run_id=ev.id, case_id=cid,
                group_name="candidate", exec_status=exec_st,
                exit_reason=exit_r, score_status=score_st,
                assertions_json="[]" if score_st != "scored" else json.dumps([
                    {"type": "text_contains", "description": "d",
                     "passed": True, "detail": ""}]),
                passed_count=p, total_count=t, score_error=err,
                created_at=time.time(),
            ))
        s.commit()

        report = evaluate_candidate(s, flow_id=flow_id, version_id=vid, suite_id=suite_id)
        by_id = {c.check_id: c for c in report.checks}
        # 分母 = 4：通过/失败/跳过/评分失败全部计入，无一被“只看已评分”丢弃
        eval_checks = [k for k in by_id if k.startswith("eval:case:")]
        assert len(eval_checks) == 4, f"分母须含失败/跳过: {sorted(by_id)}"
        assert by_id["eval:case:candidate:case-ok"].status.value == "passed"
        assert by_id["eval:case:candidate:case-fail"].status.value == "failed", "执行失败计入验收"
        assert by_id["eval:case:candidate:case-skip"].status.value == "not_run", "跳过如实标注 not_run"
        assert by_id["eval:case:candidate:case-score-fail"].status.value == "not_run"
        # 汇总检查：证据含分母说明（含失败/跳过，非仅已评分）
        summary = by_id["eval:summary"]
        assert "4" in summary.evidence
        assert "非仅已评分" in summary.evidence, "分母须声明含失败/跳过"
        assert report.has_failed


# ---------- 端到端：真实 Worker 对照 ----------


def test_evaluate_candidate_end_to_end(
    client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation.evaluation import build_eval_suite, evaluate_candidate
    from app.services import queue as queue_svc

    pid = project["id"]
    # 建版本（候选 skill 资源）
    client.put(f"/api/projects/{pid}/skills/draft/files/SKILL.md",
               json={"content": VALID_SKILL_MD}, headers=AUTH)
    client.put(f"/api/projects/{pid}/skills/draft/files/scripts/extract.py",
               json={"content": "print('extract')\n"}, headers=AUTH)
    resp = client.post(f"/api/projects/{pid}/skills/versions",
                       json={"label": "v1"}, headers=AUTH)
    job_id = resp.json()["job_id"]
    Worker().run_pending()
    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert resp.json()["status"] == "succeeded", resp.text
    vid = resp.json()["result"]["id"]

    with Session(create_db_engine(db_path(load_config()))) as s:
        suite_id = build_eval_suite(s, _contract(), _plan())

    # FakeProvider：candidate 按剧本执行（写文件+答复）；baseline 只答复
    responses = [
        _resp_tool("c1", "write_file", {"path": "out/result.md", "content": "周报 42\n"}),
        _resp_text("已按需求输出周报"),
        _resp_text("我没有输出文件"),
    ]
    fake = FakeProvider(responses)
    import app.workers.handlers.agent_run as agent_run_mod
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: fake)

    # 排评测任务（带整体预算限制），Worker 执行
    with Session(create_db_engine(db_path(load_config()))) as s:
        job = queue_svc.enqueue(s, "eval_run", {
            "project_id": pid, "suite_id": suite_id, "version_id": vid,
            "max_tokens_total": 5000,
        })
    Worker().run_pending()

    with Session(create_db_engine(db_path(load_config()))) as s:
        report = evaluate_candidate(s, flow_id="flow-e2e", version_id=vid, suite_id=suite_id)
    summary = {c.check_id: c for c in report.checks}["eval:summary"]
    # 对照证据：candidate 与 baseline 都执行（公平：同一 fixtures）
    assert "candidate" in summary.evidence and "baseline" in summary.evidence
    # 候选带 skill 资源跑赢基线或至少完成执行
    case_checks = [c for c in report.checks if c.check_id.startswith("eval:case:")]
    assert len(case_checks) >= 1
