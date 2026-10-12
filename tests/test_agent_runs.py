"""S5 测试：模型适配层 + API 执行器 v0（S5 完成定义）。

完成定义逐条映射：
- 完成一轮含脚本执行的工具循环 → test_tool_loop_with_script_execution
- 预算耗尽 → test_exit_budget_exceeded
- 工具超时 → test_exit_tool_timeout
- 模型失败 → test_exit_model_failed
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.executors import ApiExecutor
from app.executors.base import ExitReason
from app.llm import (
    Capabilities,
    ChatMessage,
    ModelError,
    ModelResponse,
    ToolCall,
    Usage,
)
from app.llm.openai_compat import OpenAICompatProvider
from app.workers.worker import Worker

from tests.test_skills import VALID_SKILL_MD

AUTH = {"x-session-token": "test-token"}


# ---------- FakeProvider：脚本化模型响应 ----------


class FakeProvider:
    """按剧本逐条返回响应的假模型。capabilities.tools=True。"""

    capabilities = Capabilities(tools=True, streaming=False, json_mode=True, max_context_tokens=100_000)

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
        self.calls.append(list(messages))
        if not self.script:
            return ModelResponse(content="（剧本耗尽）", usage=Usage(10, 5))
        return self.script.pop(0)


def _resp_tool(call_id: str, name: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage=Usage(100, 20),
        finish_reason="tool_calls",
    )


def _resp_text(text: str) -> ModelResponse:
    return ModelResponse(content=text, usage=Usage(100, 20), finish_reason="stop")


# ---------- 工具层单元测试 ----------


def test_read_write_run_script_roundtrip(tmp_path: Path) -> None:
    """三工具在沙箱内可用：write → read → run_script。"""
    from app.tools import execute_tool

    out = execute_tool(tmp_path, "write_file", {"path": "scripts/demo.py", "content": "print('hi from demo')\n"})
    assert json.loads(out)["ok"] is True
    out = execute_tool(tmp_path, "read_file", {"path": "scripts/demo.py"})
    assert "hi from demo" in out
    out = execute_tool(tmp_path, "run_script", {"path": "scripts/demo.py"})
    data = json.loads(out)
    assert data["exit_code"] == 0
    assert "hi from demo" in data["stdout"]


def test_tool_path_traversal_rejected(tmp_path: Path) -> None:
    """路径穿越在工具层被拒（返回错误 JSON，不越出沙箱）。"""
    from app.tools import execute_tool

    for bad in ["../evil.md", "a/../../evil.py", "/etc/passwd", "C:/Windows/win.ini"]:
        out = execute_tool(tmp_path, "read_file", {"path": bad})
        data = json.loads(out)
        assert data.get("error") in ("path_rejected", "file_not_found"), (bad, data)
        assert not (tmp_path.parent / "evil.md").exists()
        assert not (tmp_path.parent / "evil.py").exists()

    # 反斜杠路径同样被拒
    out = execute_tool(tmp_path, "write_file", {"path": "..\\evil.md", "content": "x"})
    assert json.loads(out).get("error") == "path_rejected"
    assert not (tmp_path / "evil.md").exists()

    # run_script 白名单外扩展名（.md 允许存在但不可作为脚本执行）
    (tmp_path / "evil.md").write_text("malware", encoding="utf-8")
    out = execute_tool(tmp_path, "run_script", {"path": "evil.md"})
    assert json.loads(out)["error"] == "script_type_not_allowed"

    # run_script 穿越同样被拒
    out = execute_tool(tmp_path, "run_script", {"path": "../evil.sh"})
    assert json.loads(out)["error"] == "path_rejected"


def test_run_script_timeout(tmp_path: Path) -> None:
    """超时脚本 → ToolTimeout。"""
    from app.tools import ToolTimeout, execute_tool

    script = "import time\ntime.sleep(120)\n" if sys.platform != "win32" else "import time\ntime.sleep(120)\n"
    (tmp_path / "slow.py").write_text(script, encoding="utf-8")
    # 临时缩短超时
    import app.tools.registry as reg
    old = reg.SCRIPT_TIMEOUT_SECONDS
    reg.SCRIPT_TIMEOUT_SECONDS = 2
    try:
        with pytest.raises(ToolTimeout):
            execute_tool(tmp_path, "run_script", {"path": "slow.py"})
    finally:
        reg.SCRIPT_TIMEOUT_SECONDS = old


# ---------- 执行器单元测试（三种退出原因 + 完整循环） ----------


def test_tool_loop_with_script_execution(tmp_path: Path) -> None:
    """S5 完成定义：一轮含脚本执行的工具循环。"""
    provider = FakeProvider([
        _resp_tool("c1", "write_file", {"path": "calc.py", "content": "print(6 * 7)\n"}),
        _resp_tool("c2", "run_script", {"path": "calc.py"}),
        _resp_text("脚本输出 42，任务完成。"),
    ])
    executor = ApiExecutor(
        provider,
        system_prompt="sys",
        user_prompt="运行计算",
        sandbox=tmp_path,
    )
    result = executor.run()
    assert result.exit_reason == ExitReason.COMPLETED
    assert result.final_message == "脚本输出 42，任务完成。"
    assert (tmp_path / "calc.py").is_file()
    kinds = [s.kind for s in result.steps]
    assert kinds == ["model", "tool", "model", "tool", "model"]
    tool_steps = [s for s in result.steps if s.kind == "tool"]
    assert tool_steps[1].name == "run_script"
    assert '"42"' in (tool_steps[1].output or "") or "42" in (tool_steps[1].output or "")
    assert result.total_usage.total_tokens == 3 * 120
    assert result.sandbox_files["text_contents"]["calc.py"] == "print(6 * 7)\n"
    assert result.sandbox_files["files"][0]["path"] == "calc.py"


def test_exit_budget_exceeded_tokens(tmp_path: Path) -> None:
    """预算耗尽（token）→ BUDGET_EXCEEDED。"""
    script = [_resp_tool(f"c{i}", "write_file", {"path": f"f{i}.md", "content": "x"}) for i in range(10)]
    script.append(_resp_text("done"))
    provider = FakeProvider(script)
    executor = ApiExecutor(
        provider,
        system_prompt="sys",
        user_prompt="任务",
        sandbox=tmp_path,
        max_tokens_total=250,  # 每轮 120 → 第二轮后耗尽
    )
    result = executor.run()
    assert result.exit_reason == ExitReason.BUDGET_EXCEEDED
    assert "token 预算耗尽" in (result.error_detail or "")


def test_exit_budget_exceeded_rounds(tmp_path: Path) -> None:
    """预算耗尽（轮次）→ BUDGET_EXCEEDED。"""
    script = [_resp_tool(f"c{i}", "write_file", {"path": f"f{i}.md", "content": "x"}) for i in range(10)]
    provider = FakeProvider(script)
    executor = ApiExecutor(
        provider,
        system_prompt="sys",
        user_prompt="任务",
        sandbox=tmp_path,
        max_tokens_total=10_000,
        max_rounds=3,
    )
    result = executor.run()
    assert result.exit_reason == ExitReason.BUDGET_EXCEEDED
    assert "轮次预算耗尽" in (result.error_detail or "")


def test_exit_tool_timeout(tmp_path: Path) -> None:
    """工具超时 → TOOL_TIMEOUT。"""
    slow = "import time\ntime.sleep(120)\n"
    provider = FakeProvider([
        _resp_tool("c1", "write_file", {"path": "slow.py", "content": slow}),
        _resp_tool("c2", "run_script", {"path": "slow.py"}),
    ])
    import app.tools.registry as reg
    old = reg.SCRIPT_TIMEOUT_SECONDS
    reg.SCRIPT_TIMEOUT_SECONDS = 2
    try:
        executor = ApiExecutor(provider, system_prompt="sys", user_prompt="任务", sandbox=tmp_path)
        result = executor.run()
    finally:
        reg.SCRIPT_TIMEOUT_SECONDS = old
    assert result.exit_reason == ExitReason.TOOL_TIMEOUT
    assert "slow.py" in (result.error_detail or "")


def test_exit_model_failed(tmp_path: Path) -> None:
    """模型失败 → MODEL_FAILED。"""

    class FailingProvider(FakeProvider):
        def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
            raise ModelError("模型 HTTP 503: upstream down", status_code=503)

    executor = ApiExecutor(
        FailingProvider([]),
        system_prompt="sys",
        user_prompt="任务",
        sandbox=tmp_path,
    )
    result = executor.run()
    assert result.exit_reason == ExitReason.MODEL_FAILED
    assert "503" in (result.error_detail or "")
    assert result.budget["usage_missing_calls"] == 1
    assert result.budget["unknown_usage_estimated_tokens"] == 4_096
    assert result.budget["accounted_tokens"] == 4_096


def test_model_failure_keeps_prior_usage_and_estimates_failed_request(tmp_path: Path) -> None:
    class FailingAfterOneProvider(FakeProvider):
        def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
            self.calls.append(list(messages))
            if self.script:
                return self.script.pop(0)
            raise ModelError("模型 HTTP 503: upstream down", status_code=503)

    provider = FailingAfterOneProvider([
        _resp_tool("c1", "write_file", {"path": "out.txt", "content": "saved"}),
    ])
    result = ApiExecutor(provider, system_prompt="sys", user_prompt="任务", sandbox=tmp_path).run()
    assert result.exit_reason == ExitReason.MODEL_FAILED
    assert result.budget["used_tokens"] == 120
    assert result.budget["usage_missing_calls"] == 1
    assert result.budget["unknown_usage_estimated_tokens"] == 4_096
    assert result.budget["accounted_tokens"] == 4_216


def test_exit_cancelled(tmp_path: Path) -> None:
    """协作取消 → CANCELLED（轮次边界检查）。"""
    provider = FakeProvider([_resp_tool(f"c{i}", "write_file", {"path": f"f{i}.md", "content": "x"}) for i in range(10)])
    executor = ApiExecutor(provider, system_prompt="sys", user_prompt="任务", sandbox=tmp_path)
    result = executor.run(is_cancelled=lambda: True)
    assert result.exit_reason == ExitReason.CANCELLED


def test_unknown_tool_returns_error_not_crash(tmp_path: Path) -> None:
    """未知工具 → 错误 JSON 回填，循环继续。"""
    provider = FakeProvider([
        _resp_tool("c1", "nonexistent_tool", {"x": 1}),
        _resp_text("工具不存在，我直接完成任务。"),
    ])
    executor = ApiExecutor(provider, system_prompt="sys", user_prompt="任务", sandbox=tmp_path)
    result = executor.run()
    assert result.exit_reason == ExitReason.COMPLETED
    tool_output = [s for s in result.steps if s.kind == "tool"][0].output or ""
    assert json.loads(tool_output)["error"] == "unknown_tool"


# ---------- OpenAI 兼容适配器单元测试 ----------


def test_openai_compat_provider_serialization() -> None:
    """消息序列化（assistant tool_calls / tool 回填）符合 OpenAI 协议。"""
    p = OpenAICompatProvider(base_url="http://localhost:1/v1", model="test-model", api_key="sk-x")
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="任务"),
        ChatMessage(role="assistant", tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.md"})]),
        ChatMessage(role="tool", tool_call_id="c1", name="read_file", content="内容"),
    ]
    serialized = p._serialize_messages(messages)
    assert serialized[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(serialized[2]["tool_calls"][0]["function"]["arguments"]) == {"path": "a.md"}
    assert serialized[3] == {"role": "tool", "tool_call_id": "c1", "content": "内容"}
    assert p._headers()["authorization"] == "Bearer sk-x"


def test_provider_from_env_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置环境变量 → ProviderNotConfigured。"""
    from app.llm import ProviderNotConfigured, provider_from_env

    monkeypatch.delenv("SKILLFORGE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("SKILLFORGE_LLM_MODEL", raising=False)
    with pytest.raises(ProviderNotConfigured):
        provider_from_env()


# ---------- API + Worker 集成（agent_run 任务） ----------


def _setup_draft(client: TestClient, pid: str) -> None:
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


def _make_version(client: TestClient, pid: str) -> str:
    _setup_draft(client, pid)
    resp = client.post(f"/api/projects/{pid}/skills/versions", json={"label": "v1"}, headers=AUTH)
    job_id = resp.json()["job_id"]
    Worker().run_pending()
    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert resp.json()["status"] == "succeeded", resp.text
    return resp.json()["result"]["id"]


class _MonkeyPatchWorker(Worker):
    """测试用 Worker：把 provider_from_env 替换为 FakeProvider 注入。

    通过 monkeypatch app.workers.handlers.agent_run.provider_from_env 实现。
    """


def _run_agent_run(client: TestClient, pid: str, body: dict) -> dict:
    resp = client.post(f"/api/projects/{pid}/runs", json=body, headers=AUTH)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    Worker().run_pending()
    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert resp.status_code == 200
    return resp.json()


def test_agent_run_job_without_provider_fails(client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置提供商 → job failed(provider_not_configured)。"""
    monkeypatch.delenv("SKILLFORGE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("SKILLFORGE_LLM_MODEL", raising=False)
    body = _run_agent_run(client, project["id"], {"prompt": "做点什么"})
    assert body["status"] == "failed"
    assert body["error"]["code"] == "provider_not_configured"


def test_agent_run_with_version_e2e(
    client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：带版本运行 → 沙箱含快照 → 工具循环完成 → run 记录落库可查。"""
    pid = project["id"]
    vid = _make_version(client, pid)

    fake = FakeProvider([
        _resp_tool("c1", "read_file", {"path": "SKILL.md"}),
        _resp_tool("c2", "write_file", {"path": "out/result.md", "content": "# 结果\n"}),
        _resp_text("我读取了技能说明并写出了结果。"),
    ])
    import app.workers.handlers.agent_run as agent_run_mod
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: fake)

    body = _run_agent_run(client, pid, {"prompt": "按技能处理", "version_id": vid, "max_tokens_total": 5000, "max_rounds": 10})
    assert body["status"] == "succeeded", body
    run = body["result"]

    assert run["exit_reason"] == "completed"
    assert run["final_message"] == "我读取了技能说明并写出了结果。"
    # 沙箱文件 = 快照文件 + 模型写出文件（F09：清单 + 文本内容分离）
    manifest = {f["path"] for f in run["sandbox_files"]["files"]}
    assert "SKILL.md" in manifest
    assert "scripts/extract.py" in manifest
    assert run["sandbox_files"]["text_contents"]["out/result.md"] == "# 结果\n"
    assert run["usage"]["prompt_tokens"] == 3 * 100

    # 单条查询与列表
    resp = client.get(f"/api/runs/{run['id']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["exit_reason"] == "completed"
    resp = client.get(f"/api/projects/{pid}/runs", headers=AUTH)
    assert len(resp.json()) == 1

    # system prompt 含技能内容
    first_call_messages = fake.calls[0]
    assert "SKILL.md" in (first_call_messages[0].content or "")
    assert "会议" in (first_call_messages[0].content or "") or "description" in (first_call_messages[0].content or "")


def test_agent_run_baseline_and_not_found(
    client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """基线运行（无版本）+ 版本不存在 → job failed(not_found)。"""
    pid = project["id"]

    fake = FakeProvider([_resp_text("基线直接回答。")])
    import app.workers.handlers.agent_run as agent_run_mod
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: fake)

    body = _run_agent_run(client, pid, {"prompt": "无技能任务"})
    assert body["status"] == "succeeded"
    assert body["result"]["exit_reason"] == "completed"
    assert body["result"]["sandbox_files"] == {"files": [], "text_contents": {}}

    # 版本不存在
    fake2 = FakeProvider([])
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: fake2)
    body = _run_agent_run(client, pid, {"prompt": "任务", "version_id": "01AAAAAAAAAAAAAAAAAAAAAAAA"})
    assert body["status"] == "failed"
    assert body["error"]["code"] == "not_found"


def test_agent_run_validation(client: TestClient, project: dict) -> None:
    """prompt 为空 → 422；项目不存在 → 404。"""
    resp = client.post(f"/api/projects/{project['id']}/runs", json={"prompt": ""}, headers=AUTH)
    assert resp.status_code == 422
    resp = client.post("/api/projects/01AAAAAAAAAAAAAAAAAAAAAAAA/runs", json={"prompt": "x"}, headers=AUTH)
    assert resp.status_code == 404
    resp = client.get("/api/runs/01AAAAAAAAAAAAAAAAAAAAAAAA", headers=AUTH)
    assert resp.status_code == 404


def test_usage_tracking_provider_accumulates_before_failed_call() -> None:
    from app.llm.usage import UsageTrackingProvider

    class PartialProvider(FakeProvider):
        def complete(self, messages, **kwargs):  # noqa: ANN001
            if len(self.calls) < 2:
                self.calls.append(list(messages))
                return _resp_text("measured")
            self.calls.append(list(messages))
            raise ModelError("upstream error")

    tracker = UsageTrackingProvider(PartialProvider([]))
    assert tracker.complete([], max_tokens=50).usage.total_tokens == 120
    assert tracker.complete([], max_tokens=60).usage.total_tokens == 120
    with pytest.raises(ModelError):
        tracker.complete([], max_tokens=70)
    meta = tracker.meta()
    assert meta["model_calls"] == 3
    assert meta["usage_calls"] == 2
    assert meta["usage_missing_calls"] == 1
    assert meta["usage"] is None
    assert meta["usage_observed"] == {"prompt_tokens": 200, "completion_tokens": 40}
    assert meta["unknown_usage_estimated_tokens"] == 70
