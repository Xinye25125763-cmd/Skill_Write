"""S6 测试：测试集 + 对照评测 + 程序断言评分。

完成定义映射：
- 端到端：brief→版本→运行→评分→结果展示 → test_eval_run_end_to_end
- 执行失败与评分失败分开显示 → test_exec_failure_vs_score_failure_separated
"""
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.llm import ModelResponse, ToolCall, Usage
from app.services.scoring import ScoringError, score_assertions
from app.workers.worker import Worker

from tests.conftest import VALID_BRIEF
from tests.test_agent_runs import AUTH, FakeProvider, _resp_text, _resp_tool
from tests.test_skills import VALID_SKILL_MD


# ---------- 评分服务单元测试 ----------


def test_score_assertions_all_types() -> None:
    sandbox = {"out/result.md": "# 结果\n42 行动项", "SKILL.md": "..."}
    assertions = [
        {"type": "file_exists", "path": "out/result.md"},
        {"type": "file_exists", "path": "nope.md"},
        {"type": "file_contains", "path": "out/result.md", "contains": "行动项"},
        {"type": "file_contains", "path": "out/result.md", "contains": "不存在的内容"},
        {"type": "text_contains", "contains": "42"},
        {"type": "exit_reason_is", "value": "completed"},
        {"type": "exit_reason_is", "value": "cancelled"},
    ]
    results = score_assertions(
        assertions, sandbox_files=sandbox, final_message="答案是 42", exit_reason="completed"
    )
    passed = [r["passed"] for r in results]
    assert passed == [True, False, True, False, True, True, False]
    assert results[1]["detail"]
    assert results[3]["detail"]


def test_score_assertions_unknown_type_fails_gracefully() -> None:
    """未知断言类型 → 单条 failed，不打断其余。"""
    results = score_assertions(
        [{"type": "magic"}, {"type": "text_contains", "contains": "x"}],
        sandbox_files={},
        final_message="x",
        exit_reason="completed",
    )
    assert results[0]["passed"] is False
    assert "未知断言类型" in results[0]["detail"]
    assert results[1]["passed"] is True


def test_score_assertions_structure_error() -> None:
    """断言整体结构非法 → ScoringError（评分失败，与断言不通过分离）。"""
    with pytest.raises(ScoringError):
        score_assertions("not-a-list", sandbox_files={}, final_message=None, exit_reason=None)
    with pytest.raises(ScoringError):
        score_assertions(["not-a-dict"], sandbox_files={}, final_message=None, exit_reason=None)


# ---------- 测试集 API ----------


def _suite_body() -> dict:
    return {
        "name": "默认测试集",
        "cases": [
            {
                "id": "case-1",
                "name": "写结果文件",
                "prompt": "把结果写到 out/result.md",
                "assertions": [
                    {"type": "file_exists", "path": "out/result.md"},
                    {"type": "text_contains", "contains": "42"},
                ],
            }
        ],
    }


def test_suite_crud_and_validation(client: TestClient, project: dict) -> None:
    pid = project["id"]
    resp = client.post(f"/api/projects/{pid}/suites", json=_suite_body(), headers=AUTH)
    assert resp.status_code == 201, resp.text
    suite = resp.json()
    assert suite["cases"][0]["id"] == "case-1"

    resp = client.get(f"/api/projects/{pid}/suites", headers=AUTH)
    assert len(resp.json()) == 1
    resp = client.get(f"/api/suites/{suite['id']}", headers=AUTH)
    assert resp.json()["name"] == "默认测试集"

    # 校验失败：未知断言类型 / 重复用例 ID
    body = _suite_body()
    body["cases"][0]["assertions"].append({"type": "magic"})
    resp = client.post(f"/api/projects/{pid}/suites", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert any("未知" in e for e in resp.json()["detail"]["errors"])

    body = _suite_body()
    body["cases"].append(dict(body["cases"][0]))
    resp = client.post(f"/api/projects/{pid}/suites", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert any("重复" in e for e in resp.json()["detail"]["errors"])

    # 空用例 / 项目不存在
    resp = client.post(f"/api/projects/{pid}/suites", json={"name": "x", "cases": []}, headers=AUTH)
    assert resp.status_code == 422
    resp = client.post(
        "/api/projects/01AAAAAAAAAAAAAAAAAAAAAAAA/suites", json=_suite_body(), headers=AUTH
    )
    assert resp.status_code == 404


# ---------- 对照评测端到端 ----------


def _make_version(client: TestClient, pid: str) -> str:
    client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": VALID_SKILL_MD},
        headers=AUTH,
    )
    client.put(
        f"/api/projects/{pid}/skills/draft/files/scripts/extract.py",
        json={"content": "print('extract')\n"},
        headers=AUTH,
    )
    resp = client.post(f"/api/projects/{pid}/skills/versions", json={"label": "v1"}, headers=AUTH)
    job_id = resp.json()["job_id"]
    Worker().run_pending()
    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert resp.json()["status"] == "succeeded", resp.text
    return resp.json()["result"]["id"]


def _run_eval(client: TestClient, pid: str, body: dict) -> dict:
    resp = client.post(f"/api/projects/{pid}/eval-runs", json=body, headers=AUTH)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    Worker().run_pending()
    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert resp.status_code == 200
    return resp.json()


def test_eval_run_end_to_end(client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：brief→版本→套件→对照运行→评分→结果视图（S6 完成定义）。"""
    pid = project["id"]

    # brief 确认（完成定义链路起点）
    resp = client.post(f"/api/projects/{pid}/briefs", json={"brief": VALID_BRIEF}, headers=AUTH)
    draft_rev = resp.json()["revision_id"]
    resp = client.patch(
        f"/api/projects/{pid}/briefs/{draft_rev}", json={"edits": {}}, headers=AUTH
    )
    assert resp.json()["status"] == "confirmed"

    vid = _make_version(client, pid)

    resp = client.post(f"/api/projects/{pid}/suites", json=_suite_body(), headers=AUTH)
    suite_id = resp.json()["id"]

    # FakeProvider：候选写文件并答复 42；基线只答复（不写文件）
    responses = [
        # candidate: case-1 → write out/result.md
        _resp_tool("c1", "write_file", {"path": "out/result.md", "content": "# 结果\n42\n"}),
        _resp_text("已写入结果文件，包含 42 个行动项。"),
        # baseline: case-1 → 直接答复
        _resp_text("我没有写文件，但答案和 42 有关。"),
    ]
    fake = FakeProvider(responses)
    import app.workers.handlers.agent_run as agent_run_mod
    monkeypatch.setattr(agent_run_mod, "provider_from_env", lambda: fake)

    body = _run_eval(client, pid, {"suite_id": suite_id, "version_id": vid, "max_tokens_total": 5000})
    assert body["status"] == "succeeded", body
    view = body["result"]

    assert view["status"] == "finished"
    # 汇总：candidate 2/2 通过；baseline 1/2（写了 final_message 含 42，但没写文件）
    assert view["summary"]["candidate"]["passed_total"] == 2
    assert view["summary"]["candidate"]["assertions_total"] == 2
    assert view["summary"]["baseline"]["passed_total"] == 1
    assert view["summary"]["baseline"]["assertions_total"] == 2

    # 逐例结果
    case_results = view["case_results"]
    assert len(case_results) == 2
    cand = next(c for c in case_results if c["group"] == "candidate")
    base = next(c for c in case_results if c["group"] == "baseline")
    assert cand["exec_status"] == "executed"
    assert cand["exit_reason"] == "completed"
    assert cand["score_status"] == "scored"
    assert cand["passed_count"] == 2
    assert base["score_status"] == "scored"
    assert base["passed_count"] == 1
    failed = [a for a in base["assertions"] if not a["passed"]]
    assert failed and "out/result.md" in failed[0]["detail"]

    # 系统提示词区分：候选首轮含 SKILL.md 内容，基线是通用提示
    assert "SKILL.md" in (fake.calls[0][0].content or "")
    assert "SKILL.md" not in (fake.calls[2][0].content or "")

    # run 视图可查（结果展示）
    resp = client.get(f"/api/eval-runs/{view['id']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["summary"]["candidate"]["passed_total"] == 2
    resp = client.get(f"/api/projects/{pid}/eval-runs", headers=AUTH)
    assert len(resp.json()) == 1


def test_exec_failure_vs_score_failure_separated(
    client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """执行失败与评分失败分开显示（S6 完成定义）。

    - 用例 A：模型失败（MODEL_FAILED）→ exec_failed + not_scored
    - 用例 B：断言结构非法（字符串而非数组）→ executed + score_failed
    - 用例 C：正常执行评分 → executed + scored
    """
    pid = project["id"]
    vid = _make_version(client, pid)

    suite_body = {
        "name": "分离测试",
        "cases": [
            {"id": "a", "prompt": "会失败的任务", "assertions": []},
            {"id": "b", "prompt": "评分会失败的任务", "assertions": [{"type": "file_exists", "path": "x.md"}]},
            {"id": "c", "prompt": "正常任务", "assertions": [{"type": "text_contains", "contains": "ok"}]},
        ],
    }
    resp = client.post(f"/api/projects/{pid}/suites", json=suite_body, headers=AUTH)
    suite_id = resp.json()["id"]

    # 用例 B 的断言改为非法结构（直接改库，构造评分失败场景）
    from app.models import Suite as SuiteModel
    from app.api.projects import get_session
    with next(get_session()) as s:
        suite = s.get(SuiteModel, suite_id)
        cases = json.loads(suite.cases_json)
        cases[1]["assertions"] = "not-a-list"  # 结构非法
        suite.cases_json = json.dumps(cases, ensure_ascii=False)
        s.commit()

    class MixedProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.n = 0

        def complete(self, messages: list, **kwargs: Any) -> ModelResponse:  # noqa: ANN001
            from app.llm import ModelError

            self.n += 1
            self.calls.append(list(messages))
            # 用例 A（candidate 第 1 次调用）失败
            if self.n == 1:
                raise ModelError("模型 HTTP 500: dead")
            return _resp_text("ok 完成")

    import app.workers.handlers.agent_run as agent_run_mod
    mixed = MixedProvider()
    monkeypatch.setattr(agent_run_mod, "provider_from_env", lambda: mixed)

    body = _run_eval(client, pid, {"suite_id": suite_id, "version_id": vid, "max_tokens_total": 5000})
    assert body["status"] == "succeeded", body
    view = body["result"]

    results = {(c["case_id"], c["group"]): c for c in view["case_results"]}
    # A-candidate：模型失败 → exec_failed，不评分
    a_c = results[("a", "candidate")]
    assert a_c["exec_status"] == "exec_failed"
    assert a_c["exit_reason"] == "model_failed"
    assert a_c["score_status"] == "not_scored"
    # A-baseline：正常执行 → scored
    a_b = results[("a", "baseline")]
    assert a_b["exec_status"] == "executed"
    assert a_b["score_status"] == "scored"
    # B：执行成功但评分失败（结构非法）
    for group in ("candidate", "baseline"):
        b = results[("b", group)]
        assert b["exec_status"] == "executed"
        assert b["exit_reason"] == "completed"
        assert b["score_status"] == "score_failed"
        assert "数组" in (b["score_error"] or "")
    # C：正常
    c_c = results[("c", "candidate")]
    assert c_c["exec_status"] == "executed"
    assert c_c["score_status"] == "scored"
    assert c_c["passed_count"] == 1

    # 汇总分开计数（按组统计：candidate 组只有本组的 6 个结果）
    assert view["summary"]["candidate"]["exec_failed"] == 1
    assert view["summary"]["candidate"]["score_failed"] == 1
    assert view["summary"]["candidate"]["scored"] == 1
    # baseline 组：a/c 正常评分，b 评分失败
    assert view["summary"]["baseline"]["exec_failed"] == 0
    assert view["summary"]["baseline"]["score_failed"] == 1
    assert view["summary"]["baseline"]["scored"] == 2


def test_eval_run_not_found_and_validation(client: TestClient, project: dict) -> None:
    """套件/版本不存在 → 404 或 job failed(not_found)。"""
    pid = project["id"]
    vid = _make_version(client, pid)
    resp = client.post(f"/api/projects/{pid}/suites", json=_suite_body(), headers=AUTH)
    suite_id = resp.json()["id"]

    # 套件不存在 → 404
    resp = client.post(
        f"/api/projects/{pid}/eval-runs",
        json={"suite_id": "01AAAAAAAAAAAAAAAAAAAAAAAA", "version_id": vid},
        headers=AUTH,
    )
    assert resp.status_code == 404

    # 版本不存在 → job failed(not_found)
    import app.workers.handlers.agent_run as agent_run_mod
    from tests.test_agent_runs import FakeProvider as FP

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_run_mod, "provider_from_env", lambda: FP([]))
    try:
        body = _run_eval(client, pid, {"suite_id": suite_id, "version_id": "01AAAAAAAAAAAAAAAAAAAAAAAA"})
        assert body["status"] == "failed"
        assert body["error"]["code"] == "not_found"
    finally:
        monkeypatch.undo()

    # 查询不存在的 eval-run → 404
    resp = client.get("/api/eval-runs/01AAAAAAAAAAAAAAAAAAAAAAAA", headers=AUTH)
    assert resp.status_code == 404
