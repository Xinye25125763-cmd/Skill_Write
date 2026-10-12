"""第二批缺陷回归测试：F07 / F08 / F10（执行器退出语义与收尾）。

- F07：finish_reason=length → TRUNCATED（不再标 completed）；
  content_filter → MODEL_FAILED。
- F08：工具意外异常结构化回填（循环继续）；非对象参数校验；
  agent_run 意外异常收尾（run 不留 running）；provider 未配置不遗留 run 行；
  eval_run 单例异常不拖垮整批。
- F10：单轮用量推超总预算且未正常结束 → BUDGET_EXCEEDED（不再 completed）。
"""
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.executors import ApiExecutor
from app.executors.base import ExitReason
from app.llm import Capabilities, ChatMessage, ModelResponse, ToolCall, Usage
from app.services import queue
from app.workers.worker import Worker

from tests.test_skills import VALID_SKILL_MD

AUTH = {"x-session-token": "test-token"}


def _make_version(client: TestClient, pid: str) -> str:
    """写草稿 → 快照 v1 → 返回版本 id（对照评测需要候选版本）。"""
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


class FakeProvider:
    capabilities = Capabilities(tools=True, streaming=False, json_mode=True, max_context_tokens=100_000)

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
        self.calls.append(list(messages))
        if not self.script:
            return ModelResponse(content="（剧本耗尽）", usage=Usage(10, 5), finish_reason="stop")
        return self.script.pop(0)


def _resp_tool(call_id: str, name: str, arguments: Any) -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage=Usage(100, 20),
        finish_reason="tool_calls",
    )


def _run(sandbox: Path, script: list[ModelResponse], **kw: Any):
    provider = FakeProvider(script)
    executor = ApiExecutor(provider, system_prompt="s", user_prompt="u", sandbox=sandbox, **kw)
    return provider, executor.run()


# ---------- F07 ----------


def test_f07_truncated_not_completed(tmp_path: Path) -> None:
    """finish_reason=length 的无工具响应 → TRUNCATED（原缺陷：completed）。"""
    _, result = _run(tmp_path, [ModelResponse(content="部分输出", usage=Usage(10, 5), finish_reason="length")])
    assert result.exit_reason == ExitReason.TRUNCATED
    assert "截断" in (result.error_detail or "")


def test_f07_content_filter_not_completed(tmp_path: Path) -> None:
    """finish_reason=content_filter → MODEL_FAILED 带证据。"""
    _, result = _run(tmp_path, [ModelResponse(content=None, usage=Usage(10, 5), finish_reason="content_filter")])
    assert result.exit_reason == ExitReason.MODEL_FAILED
    assert "content_filter" in (result.error_detail or "")


def test_f07_stop_still_completed(tmp_path: Path) -> None:
    """finish_reason=stop 无工具调用 → COMPLETED（正常路径不回归）。"""
    _, result = _run(tmp_path, [ModelResponse(content="完成了", usage=Usage(10, 5), finish_reason="stop")])
    assert result.exit_reason == ExitReason.COMPLETED
    assert result.final_message == "完成了"


def test_f07_no_finish_reason_completed(tmp_path: Path) -> None:
    """提供商未返回 finish_reason（兼容面）→ 仍 COMPLETED。"""
    _, result = _run(tmp_path, [ModelResponse(content="完成了", usage=Usage(10, 5))])
    assert result.exit_reason == ExitReason.COMPLETED


# ---------- F10 ----------


def test_f10_overspend_not_completed(tmp_path: Path) -> None:
    """单轮用量推超总预算且非 stop → BUDGET_EXCEEDED（原缺陷：completed）。

    报告复现场景：预算 100，响应用量 115。
    """
    _, result = _run(
        tmp_path,
        [ModelResponse(content="部分输出", usage=Usage(100, 15), finish_reason="length")],
        max_tokens_total=100,
    )
    assert result.exit_reason == ExitReason.BUDGET_EXCEEDED


def test_f10_overspend_with_stop_still_completed(tmp_path: Path) -> None:
    """超预算但模型已正常收尾（finish_reason=stop）→ 仍 COMPLETED。

    语义：模型自己声明完成，预算只影响"是否继续"，不追溯否定已完成的输出。
    """
    _, result = _run(
        tmp_path,
        [ModelResponse(content="完成了", usage=Usage(100, 15), finish_reason="stop")],
        max_tokens_total=100,
    )
    assert result.exit_reason == ExitReason.COMPLETED


# ---------- F08：执行器工具兜底 ----------


def test_f08_tool_unexpected_exception_fed_back(tmp_path: Path) -> None:
    """write_file 写到已存在目录路径 → IsADirectoryError 兜底为错误 JSON，
    循环继续，下一轮模型给出最终答复（原缺陷：异常逃逸终止一切）。"""
    (tmp_path / "adir").mkdir()
    provider = FakeProvider([
        _resp_tool("c1", "write_file", {"path": "adir", "content": "x"}),  # 目标是目录
        ModelResponse(content="换路径重写了", usage=Usage(50, 10), finish_reason="stop"),
    ])
    executor = ApiExecutor(provider, system_prompt="s", user_prompt="u", sandbox=tmp_path)
    result = executor.run()
    assert result.exit_reason == ExitReason.COMPLETED
    assert result.final_message == "换路径重写了"
    # 错误以 tool_failed JSON 回填进第二轮的模型上下文
    tool_msgs = [m for m in provider.calls[1] if m.role == "tool"]
    assert tool_msgs and json.loads(tool_msgs[0].content)["error"] == "tool_failed"


def test_f08_non_object_arguments_fed_back(tmp_path: Path) -> None:
    """工具参数为 JSON 数组 → invalid_arguments 错误回填，循环继续。"""
    provider = FakeProvider([
        _resp_tool("c1", "write_file", ["not", "a", "dict"]),
        ModelResponse(content="参数写错了，重试", usage=Usage(50, 10), finish_reason="stop"),
    ])
    executor = ApiExecutor(provider, system_prompt="s", user_prompt="u", sandbox=tmp_path)
    result = executor.run()
    assert result.exit_reason == ExitReason.COMPLETED
    tool_msgs = [m for m in provider.calls[1] if m.role == "tool"]
    assert tool_msgs and json.loads(tool_msgs[0].content)["error"] == "invalid_arguments"


# ---------- F08③：agent_run 收尾（job 级） ----------


def _run_agent_job(client: TestClient, cfg, monkeypatch, pid: str, provider, prompt: str = "p"):
    from app.workers.handlers import agent_run as agent_run_mod

    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: provider)
    r = client.post(f"/api/projects/{pid}/runs", json={"prompt": prompt}, headers=AUTH)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    Worker().run_pending()
    return client.get(f"/api/jobs/{job_id}", headers=AUTH).json()


def _mk_project(client: TestClient) -> str:
    r = client.post("/api/projects", json={"name": "b2"}, headers=AUTH)
    return r.json()["id"]


class ExplodingProvider:
    """第 1 轮抛非 ModelError 的意外异常（穿透执行器旧版 except ModelError）。"""

    capabilities = Capabilities(tools=True, streaming=False, json_mode=True, max_context_tokens=100_000)

    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
        raise RuntimeError("意外的模型崩溃")


def test_f08_agent_run_unexpected_exception_closes_run(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """执行器意外异常 → run 行以 model_failed 收尾（不留 running），job succeeded。"""
    cfg = load_config()
    pid = _mk_project(client)
    job = _run_agent_job(client, cfg, monkeypatch, pid, ExplodingProvider())
    assert job["status"] == "succeeded", job
    run = job["result"]
    assert run["exit_reason"] == "model_failed"
    assert "意外异常" in (run["error_detail"] or "")
    assert run["status"] == "finished"
    # run 记录查询同样收尾
    one = client.get(f"/api/runs/{run['id']}", headers=AUTH).json()
    assert one["status"] == "finished"


def test_f08_provider_not_configured_leaves_no_running_row(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider 未配置 → job failed（provider_not_configured），不遗留 running 行。"""
    from app.workers.handlers import agent_run as agent_run_mod

    def unconfigured():
        from app.llm import ProviderNotConfigured

        raise ProviderNotConfigured("SKILLFORGE_LLM_* 未配置")

    monkeypatch.setattr(agent_run_mod, "provider_factory", unconfigured)
    pid = _mk_project(client)
    r = client.post(f"/api/projects/{pid}/runs", json={"prompt": "p"}, headers=AUTH)
    job_id = r.json()["job_id"]
    Worker().run_pending()
    job = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
    assert job["status"] == "failed"
    assert job["error"]["code"] == "provider_not_configured"

    # 无遗留 running 的 AgentRun 行
    from app.models import AgentRun

    with Session(create_db_engine(db_path(cfg := load_config()))) as s:
        assert s.query(AgentRun).filter_by(status="running").count() == 0
        assert s.query(AgentRun).count() == 0


# ---------- F08④：eval_run 单例隔离 ----------


def test_f08_eval_single_case_failure_does_not_kill_batch(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评测中某用例执行异常 → 该例 exec_failed，其余用例与汇总正常完成。"""
    from app.workers.handlers import agent_run as agent_run_mod

    cfg = load_config()
    pid = _mk_project(client)
    # 建套件：3 用例
    cases = [
        {"id": "a", "prompt": "任务A", "assertions": [{"type": "exit_reason_is", "value": "completed"}]},
        {"id": "b", "prompt": "任务B", "assertions": [{"type": "exit_reason_is", "value": "completed"}]},
        {"id": "c", "prompt": "任务C", "assertions": [{"type": "exit_reason_is", "value": "completed"}]},
    ]
    r = client.post(f"/api/projects/{pid}/suites", json={"name": "s", "cases": cases}, headers=AUTH)
    assert r.status_code == 201, r.text
    suite_id = r.json()["id"]
    version_id = _make_version(client, pid)
    r = client.post(
        f"/api/projects/{pid}/eval-runs",
        json={"suite_id": suite_id, "version_id": version_id},
        headers=AUTH,
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    # provider 剧本：顺序为 a候选=1、a基线=2、b候选=3、b基线=4、c候选=5、c基线=6，
    # 第 3、4 次（用例 b 的两组）爆炸 → 仅 b 两例 exec_failed
    class HalfBroken:
        capabilities = Capabilities(tools=True, streaming=False, json_mode=True, max_context_tokens=100_000)
        n = 0

        def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
            type(self).n += 1
            if type(self).n in (3, 4):
                raise RuntimeError("这个用例的模型崩了")
            return ModelResponse(content="好的", usage=Usage(50, 10), finish_reason="stop")

    provider = HalfBroken()
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: provider)
    Worker().run_pending()
    job = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
    assert job["status"] == "succeeded", job  # 单例失败不拖垮评测
    view = job["result"]
    # 每组 3 例全有行
    for group in ("candidate", "baseline"):
        rows = [c for c in view["case_results"] if c["group"] == group]
        assert len(rows) == 3
    # 无 running 遗留的 AgentRun
    from app.models import AgentRun

    with Session(create_db_engine(db_path(cfg))) as s:
        assert s.query(AgentRun).filter_by(status="running").count() == 0
