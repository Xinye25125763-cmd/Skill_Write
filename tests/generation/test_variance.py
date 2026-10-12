"""v1.4 R2：评测方差分析专项验收（需求文档 §3.4）。

覆盖：
1. 纯函数层：多数决边界（1/3、2/3、平票）、flaky、non-discriminating、
   组级 pass_rate±stdev 与 delta；
2. 端到端：套件 repeats=3 → attempt 序号落库、多数决折算、flaky 标注入
   eval:case 证据与 eval:summary 信号、non-discriminating 用例浮出；
3. 回归零破坏：repeats 缺省（=1）时行为与旧版一致（attempt=1）。
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
from app.generation import variance as V
from app.models import EvalCaseResult, EvalRun, Suite
from app.workers.worker import Worker

from tests.test_agent_runs import AUTH, FakeProvider, _resp_text, _resp_tool
from tests.test_skills import VALID_SKILL_MD


# ---------- 纯函数层 ----------

def _att(outcome: str, attempt: int = 1) -> dict[str, Any]:
    """构造一次尝试的逻辑视图行（按结论反推状态字段）。"""
    if outcome == "passed":
        return {"exec_status": "executed", "exit_reason": "completed",
                "score_status": "scored", "passed_count": 2, "total_count": 2,
                "attempt": attempt}
    if outcome == "failed":
        return {"exec_status": "executed", "exit_reason": "model_failed",
                "score_status": "not_scored", "passed_count": 0, "total_count": 0,
                "attempt": attempt}
    return {"exec_status": "skipped", "exit_reason": None,
            "score_status": "not_scored", "passed_count": 0, "total_count": 0,
            "attempt": attempt}


def test_majority_boundaries() -> None:
    """多数决：2/3 过；1/3 不过；平票算 failed（保守）；全 not_run → not_run。"""
    assert V.majority_outcome([_att("passed"), _att("passed"), _att("failed")]) == "passed"
    assert V.majority_outcome([_att("failed"), _att("failed"), _att("passed")]) == "failed"
    assert V.majority_outcome([_att("passed"), _att("failed")]) == "failed", "平票保守算 failed"
    assert V.majority_outcome([_att("not_run")]) == "not_run"
    # passed 与 not_run 平票（1:1）：not_run 不冒充结论，passed 不过半 → failed 保守
    assert V.majority_outcome([_att("passed"), _att("not_run")]) == "failed"
    assert V.majority_outcome([]) == "not_run"


def test_flaky_and_non_discriminating() -> None:
    """flaky：尝试>1 且结论翻转；non-discriminating：两组同用例都过。"""
    assert not V.is_flaky([_att("passed")]), "单次尝试无从翻转"
    assert V.is_flaky([_att("passed"), _att("failed"), _att("passed")])
    assert not V.is_flaky([_att("failed"), _att("failed")]), "一致失败不是 flaky"

    rows: list[dict[str, Any]] = [
        # case-both-pass：两组都过 → non-discriminating
        {"group_name": "candidate", "case_id": "case-both-pass", **_att("passed")},
        {"group_name": "baseline", "case_id": "case-both-pass", **_att("passed")},
        # case-cand-only：只有候选过 → 区分性用例
        {"group_name": "candidate", "case_id": "case-cand-only", **_att("passed")},
        {"group_name": "baseline", "case_id": "case-cand-only", **_att("failed")},
        # case-both-fail：都失败不算（不是虚增通过率）
        {"group_name": "candidate", "case_id": "case-both-fail", **_att("failed")},
        {"group_name": "baseline", "case_id": "case-both-fail", **_att("failed")},
    ]
    stats = V.group_case_stats(rows)
    assert V.non_discriminating(stats) == ["case-both-pass"]


def test_group_summary_stdev_and_delta() -> None:
    """组级统计：pass_rate 均值±stdev、delta、not_run 不进均值分母。"""
    rows = [
        {"group_name": "candidate", "case_id": "c1", **_att("passed")},
        {"group_name": "candidate", "case_id": "c2", **_att("failed")},
        {"group_name": "candidate", "case_id": "c3", **_att("not_run")},
        {"group_name": "baseline", "case_id": "c1", **_att("failed")},
        {"group_name": "baseline", "case_id": "c2", **_att("failed")},
        {"group_name": "baseline", "case_id": "c3", **_att("failed")},
    ]
    stats = V.group_case_stats(rows)
    summary = V.group_summary(stats)
    cand = summary["candidate"]
    assert cand["cases"] == 3 and cand["scored_cases"] == 2 and cand["not_run_cases"] == 1
    assert cand["pass_rate"] == 0.5, "not_run 不进均值分母"
    assert cand["pass_rate_stdev"] == 0.5, "两点 {1.0, 0.0} 的总体标准差"
    assert summary["baseline"]["pass_rate"] == 0.0
    assert V.delta(summary) == 0.5
    # 单组缺失 → delta None
    assert V.delta({"candidate": cand}) is None


# ---------- 端到端：repeats 落库 + 折算 ----------

def _make_version(client: TestClient, pid: str) -> str:
    client.put(f"/api/projects/{pid}/skills/draft/files/SKILL.md",
               json={"content": VALID_SKILL_MD}, headers=AUTH)
    client.put(f"/api/projects/{pid}/skills/draft/files/scripts/extract.py",
               json={"content": "print('extract')\n"}, headers=AUTH)
    resp = client.post(f"/api/projects/{pid}/skills/versions",
                       json={"label": "v-var"}, headers=AUTH)
    job_id = resp.json()["job_id"]
    Worker().run_pending()
    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert resp.json()["status"] == "succeeded", resp.text
    return resp.json()["result"]["id"]


def test_repeats_flaky_and_nondiscriminating_end_to_end(
    client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 1+2+4：repeats=3 → attempt 序号、多数决、flaky/信号浮出。"""
    pid = project["id"]
    vid = _make_version(client, pid)

    # 套件：case-stable（缺省 repeats=1，两组都过 → non-discriminating）
    #       case-flaky（repeats=3，候选 2/3 过 → 多数决 passed + flaky 标注）
    suite_body = {
        "name": "方差测试集",
        "cases": [
            {"id": "case-stable", "name": "稳定用例",
             "prompt": "输出包含 42 的答复",
             "assertions": [{"type": "text_contains", "contains": "42"}]},
            {"id": "case-flaky", "name": "翻转用例", "repeats": 3,
             "prompt": "把 42 写到 out/result.md",
             "assertions": [{"type": "file_exists", "path": "out/result.md"},
                            {"type": "text_contains", "contains": "42"}]},
        ],
    }
    resp = client.post(f"/api/projects/{pid}/suites", json=suite_body, headers=AUTH)
    suite_id = resp.json()["id"]

    # 剧本（执行顺序：case-stable cand → base；case-flaky cand A1/A2/A3 → base ×3）
    responses = [
        _resp_text("稳定答复：42 个行动项"),                      # stable candidate
        _resp_text("基线答复：也是 42 个"),                       # stable baseline（都过）
        _resp_tool("f1", "write_file",
                   {"path": "out/result.md", "content": "42\n"}),  # flaky cand A1
        _resp_text("A1 已写入 42"),
        _resp_text("A2 没写文件，但提到 42"),                     # flaky cand A2（失败）
        _resp_tool("f3", "write_file",
                   {"path": "out/result.md", "content": "42\n"}),  # flaky cand A3
        _resp_text("A3 已写入 42"),
        _resp_text("base A1 没写文件 42"),
        _resp_text("base A2 没写文件 42"),
        _resp_text("base A3 没写文件 42"),
    ]
    fake = FakeProvider(responses)
    import app.workers.handlers.agent_run as agent_run_mod
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: fake)

    from app.services import queue as queue_svc
    with Session(create_db_engine(db_path(load_config()))) as s:
        job = queue_svc.enqueue(s, "eval_run", {
            "project_id": pid, "suite_id": suite_id, "version_id": vid,
            "max_tokens_total": 5000,
        })
        job_id = job.id
    Worker().run_pending()

    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert resp.json()["status"] == "succeeded", resp.text
    view = resp.json()["result"]
    assert view["status"] == "finished"

    # attempt 序号与尝试计数落库
    attempts = {(c["group"], c["case_id"]): c["attempt"] for c in view["case_results"]}
    assert attempts[("candidate", "case-flaky")] == 3
    assert attempts[("baseline", "case-flaky")] == 3
    assert attempts[("candidate", "case-stable")] == 1, "缺省 repeats=1（回归零破坏）"
    assert len(view["case_results"]) == 8

    # 方差统计视图
    var = view["variance"]
    assert var["flaky_cases"]["candidate"] == ["case-flaky"]
    assert var["flaky_cases"]["baseline"] == []
    assert var["non_discriminating"] == ["case-stable"]
    assert var["groups"]["candidate"]["pass_rate"] == 1.0, "两用例多数决都过"
    assert var["groups"]["baseline"]["pass_rate"] == 0.5
    assert var["delta_pass_rate"] == 0.5

    # evaluate_candidate 折算：多数决 + flaky/信号入证据
    from app.generation.evaluation import evaluate_candidate
    with Session(create_db_engine(db_path(load_config()))) as s:
        report = evaluate_candidate(s, flow_id="flow-var", version_id=vid,
                                    suite_id=suite_id)
    by_id = {c.check_id: c for c in report.checks}
    flaky = by_id["eval:case:candidate:case-flaky"]
    assert flaky.status.value == "passed", "2/3 多数决通过"
    assert "2/3 次通过" in flaky.evidence
    assert "flaky" in flaky.evidence, "翻转用例必须带 flaky 标注"
    stable = by_id["eval:case:candidate:case-stable"]
    assert stable.status.value == "passed"
    summary = by_id["eval:summary"]
    assert "方差分析" in summary.evidence
    assert "non-discriminating 用例 ['case-stable']" in summary.evidence
    assert "flaky 用例 ['case-flaky']" in summary.evidence


def test_repeats_default_one_unchanged_behavior(
    client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 3：无 repeats 的套件 → 每用例恰一行、attempt=1（现行为）。"""
    pid = project["id"]
    vid = _make_version(client, pid)
    suite_body = {
        "name": "单次测试集",
        "cases": [
            {"id": "case-a", "name": "单次", "prompt": "答复 42",
             "assertions": [{"type": "text_contains", "contains": "42"}]},
        ],
    }
    resp = client.post(f"/api/projects/{pid}/suites", json=suite_body, headers=AUTH)
    suite_id = resp.json()["id"]

    fake = FakeProvider([_resp_text("42"), _resp_text("42")])
    import app.workers.handlers.agent_run as agent_run_mod
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: fake)

    from app.services import queue as queue_svc
    with Session(create_db_engine(db_path(load_config()))) as s:
        job = queue_svc.enqueue(s, "eval_run", {
            "project_id": pid, "suite_id": suite_id, "version_id": vid,
            "max_tokens_total": 5000,
        })
        job_id = job.id
    Worker().run_pending()

    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    view = resp.json()["result"]
    assert len(view["case_results"]) == 2, "1 用例 × 2 组 × 1 次"
    assert all(c["attempt"] == 1 for c in view["case_results"])
    assert view["variance"]["groups"]["candidate"]["cases"] == 1
