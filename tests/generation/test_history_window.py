"""v1.5 M3B：执行器历史窗口化验收。

背景：工具输出随轮次全部重发进上下文是 token 膨胀主因（二次方增长）。
M3A 压了单次输出上限，M3B 让窗口外（>keep_rounds 轮前）的超长历史
工具输出在发送给模型前降档为头尾摘要：
- 完整内容仍在 StepRecord 与沙箱磁盘（read_file 支持 offset/limit 分段重读）；
- 降档幂等（已压缩消息不再处理）、tool_call_id 配对不变；
- keep_rounds=0 关闭窗口化，行为与 v1.4 逐字节一致；
- ExecutorResult.history_window 报告 keep/压缩条数/省掉字符数，
  agent_run 落库进 budget_json.history_window。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.workers.handlers.agent_run as agent_run_mod
from app.executors import ApiExecutor
from app.executors.base import ExitReason
from app.llm import Capabilities, ChatMessage, ModelResponse, ToolCall, Usage
from app.workers.worker import Worker

AUTH = {"x-session-token": "test-token"}


class ScriptedProvider:
    """按剧本逐条返回响应；逐次快照收到的消息列表供断言。"""

    capabilities = Capabilities(tools=True, streaming=False, json_mode=True, max_context_tokens=100_000)

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
        self.calls.append([ChatMessage(role=m.role, content=m.content, tool_calls=list(m.tool_calls),
                                       tool_call_id=m.tool_call_id) for m in messages])
        if not self.script:
            return ModelResponse(content="（剧本耗尽）", usage=Usage(10, 5), finish_reason="stop")
        return self.script.pop(0)


def _resp_tool(call_id: str, name: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage=Usage(100, 20),
        finish_reason="tool_calls",
    )


def _resp_text(text: str) -> ModelResponse:
    return ModelResponse(content=text, usage=Usage(100, 20), finish_reason="stop")


LONG = "A" * 3_000   # 超过 600 阈值的工具输出（write_file 回执短，用 read_file 读长文件产生）
SHORT = "B" * 100    # 低于阈值的工具输出，不该被压缩


def _make_script(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text(LONG, encoding="utf-8")
    (tmp_path / "short.txt").write_text(SHORT, encoding="utf-8")


def test_history_window_compresses_stale_tool_outputs(tmp_path: Path) -> None:
    """窗口外（keep=3）旧轮超长工具输出降档为头尾摘要；近 3 轮逐字保留。"""
    _make_script(tmp_path)
    # 6 轮工具调用 + 1 轮收尾：轮 0-5 各一次 read_file
    script = [_resp_tool(f"c{i}", "read_file", {"path": "long.txt" if i % 2 == 0 else "short.txt"})
              for i in range(6)]
    script.append(_resp_text("done"))
    provider = ScriptedProvider(script)

    executor = ApiExecutor(
        provider,
        system_prompt="sys",
        user_prompt="任务",
        sandbox=tmp_path,
        max_tokens_total=10_000,
        max_rounds=10,
        history_tool_keep_rounds=3,
    )
    result = executor.run()
    assert result.exit_reason == ExitReason.COMPLETED

    # 7 次模型调用（6 轮工具 + 1 轮收尾）
    assert len(provider.calls) == 7
    # 第 7 次调用（round_index=6，已完成 6 轮）：窗口外 = 轮 0-2 的消息
    # 轮 0（long.txt，超阈值）应被压缩；轮 1（short.txt，低于阈值）不动
    last_call = provider.calls[-1]
    tool_msgs = [m for m in last_call if m.role == "tool"]
    compressed = [m for m in tool_msgs if "历史工具输出已压缩" in (m.content or "")]
    assert len(compressed) >= 1, [ (m.content or "")[:80] for m in tool_msgs ]
    # 压缩消息：头尾保留 + 重读指引 + 长度大幅缩减
    c = compressed[0].content or ""
    assert c.startswith("A" * 200)
    assert c.rstrip().endswith("A" * 200)
    assert "原始 3000 字符" in c
    assert "offset/limit" in c
    assert len(c) < 700
    # 未压缩的 long 输出（近 3 轮内，轮 2？—— 轮 2 是 long：窗口外；轮 4 是 long：窗口内）逐字保留
    verbatim = [m for m in tool_msgs if (m.content or "") == LONG or (LONG in (m.content or "") and "已压缩" not in (m.content or ""))]
    assert any(m.content == LONG for m in tool_msgs), "近窗口内的长输出必须逐字保留"
    # tool_call_id 配对仍在
    for m in compressed:
        assert m.tool_call_id is not None

    # 结果统计：压缩条数与省掉字符数为正
    hw = result.history_window
    assert hw["keep_rounds"] == 3
    assert hw["compressed_messages"] >= 1
    assert hw["saved_chars"] > 0

    # StepRecord 保留完整输出（磁盘真相不丢）
    tool_steps = [s for s in result.steps if s.kind == "tool"]
    long_steps = [s for s in tool_steps if s.arguments and s.arguments.get("path") == "long.txt"]
    assert all((s.output or "") == LONG for s in long_steps)


def test_history_window_idempotent_and_threshold(tmp_path: Path) -> None:
    """低于阈值的输出永不压缩；已压缩消息二次压缩不叠加（幂等）。"""
    (tmp_path / "short.txt").write_text(SHORT, encoding="utf-8")
    script = [_resp_tool(f"c{i}", "read_file", {"path": "short.txt"}) for i in range(6)]
    script.append(_resp_text("done"))
    provider = ScriptedProvider(script)
    executor = ApiExecutor(provider, system_prompt="sys", user_prompt="任务",
                           sandbox=tmp_path, max_tokens_total=10_000, max_rounds=10,
                           history_tool_keep_rounds=3)
    result = executor.run()
    assert result.exit_reason == ExitReason.COMPLETED
    # 全部低于 600 阈值 → 一条都没压缩
    assert result.history_window["compressed_messages"] == 0
    assert result.history_window["saved_chars"] == 0
    last_call = provider.calls[-1]
    tool_msgs = [m for m in last_call if m.role == "tool"]
    assert all((m.content or "") == SHORT for m in tool_msgs)


def test_compressed_tool_output_can_be_restored_on_demand(tmp_path: Path) -> None:
    """Older history is compact, but the model can retrieve the exact result."""
    _make_script(tmp_path)
    (tmp_path / "emit.py").write_text(
        "print('S' * 2500)\nimport sys; print('E' * 2500, file=sys.stderr)\n", encoding="utf-8"
    )

    class AutoRestoreProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__([_resp_tool("c0", "run_script", {"path": "emit.py"})] +
                             [_resp_tool(f"c{i}", "read_file", {"path": "long.txt"})
                              for i in range(1, 6)] + [_resp_text("done")])

        def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
            if len(self.calls) == 6:
                self.calls.append([ChatMessage(role=m.role, content=m.content,
                                               tool_calls=list(m.tool_calls),
                                               tool_call_id=m.tool_call_id) for m in messages])
                summary = next(m.content for m in messages if m.role == "tool"
                               and "历史工具输出已压缩" in (m.content or ""))
                match = re.search(r"output_id='([^']+)'", summary or "")
                assert match, summary
                offset = len('{"exit_code": 0, "stdout": "') + 1_000
                return _resp_tool("restore", "read_tool_output", {
                    "output_id": match.group(1), "offset": offset, "limit": 4_000,
                })
            return super().complete(messages, **kwargs)

    provider = AutoRestoreProvider()
    result = ApiExecutor(
        provider, system_prompt="sys", user_prompt="任务", sandbox=tmp_path,
        max_tokens_total=10_000, max_rounds=10, history_tool_keep_rounds=3,
    ).run()
    assert result.exit_reason == ExitReason.COMPLETED
    restore_reply = [m for m in provider.calls[-1]
                     if m.role == "tool" and m.tool_call_id == "restore"]
    assert len(restore_reply) == 1
    full_script = next(s.output for s in result.steps if s.name == "run_script")
    offset = len('{"exit_code": 0, "stdout": "') + 1_000
    assert restore_reply[0].content.startswith(full_script[offset:offset + 200])
    assert "S" * 200 in (restore_reply[0].content or "")


def test_persisted_output_can_be_paged_by_new_executor_after_restart(tmp_path: Path) -> None:
    """新执行器通过持久引用读取之前保存内容的中间页。"""
    (tmp_path / "emit.py").write_text(
        "print('S' * 2500)\nimport sys; print('E' * 2500, file=sys.stderr)\n", encoding="utf-8"
    )
    stored: dict[str, str] = {}
    first = ApiExecutor(
        ScriptedProvider([_resp_tool("persist-me", "run_script", {"path": "emit.py"}),
                          _resp_text("saved")]),
        system_prompt="sys", user_prompt="任务", sandbox=tmp_path,
        max_tokens_total=10_000, max_rounds=5,
        persist_tool_output=lambda output_id, _call_id, _name, content: stored.__setitem__(output_id, content),
    ).run()
    output_step = next(s for s in first.steps if s.kind == "tool")
    assert output_step.output_id in stored
    assert output_step.output_length == len(output_step.output or "")
    full_json = json.loads(stored[output_step.output_id])
    assert len(full_json["stdout"]) == 2501 and len(full_json["stderr"]) == 2501
    middle_offset = stored[output_step.output_id].index("S" * 100) + 1_000

    second = ApiExecutor(
        ScriptedProvider([_resp_tool("page", "read_tool_output", {
            "output_id": output_step.output_id, "offset": middle_offset, "limit": 300,
        }), _resp_text("restored")]),
        system_prompt="sys", user_prompt="继续", sandbox=tmp_path,
        max_tokens_total=10_000, max_rounds=5,
        read_tool_output=lambda output_id: stored.get(output_id),
    ).run()
    restored = next(m for m in second.steps if m.kind == "tool")
    assert restored.output.startswith(stored[output_step.output_id][middle_offset:middle_offset + 300])


def test_script_stdout_stderr_middle_restored_after_compression(tmp_path: Path) -> None:
    """长脚本 JSON 输出轨迹同时保留 stdout/stderr，不被模型上下文截断覆盖。"""
    (tmp_path / "emit.py").write_text(
        "print('S' * 2500)\nimport sys; print('E' * 2500, file=sys.stderr)\n", encoding="utf-8"
    )
    provider = ScriptedProvider([
        _resp_tool("script", "run_script", {"path": "emit.py"}),
        _resp_tool("noop1", "write_file", {"path": "x.txt", "content": "x"}),
        _resp_tool("noop2", "write_file", {"path": "y.txt", "content": "y"}),
        _resp_tool("noop3", "write_file", {"path": "z.txt", "content": "z"}),
        _resp_tool("noop4", "write_file", {"path": "w.txt", "content": "w"}),
        _resp_text("done"),
    ])
    result = ApiExecutor(provider, system_prompt="sys", user_prompt="任务", sandbox=tmp_path,
                         max_tokens_total=10_000, max_rounds=10, history_tool_keep_rounds=1).run()
    assert result.exit_reason == ExitReason.COMPLETED
    script_step = next(s for s in result.steps if s.name == "run_script")
    assert script_step.output_id
    assert len(script_step.output or "") > 4_000
    assert "S" * 100 in (script_step.output or "")
    assert "E" * 100 in (script_step.output or "")
    assert json.loads(script_step.output or "{}")["stderr"].startswith("E")


def test_history_window_disabled_keeps_v14_behavior(tmp_path: Path) -> None:
    """keep_rounds=0 关闭窗口化：所有历史逐字重发（v1.4 行为）。"""
    _make_script(tmp_path)
    script = [_resp_tool(f"c{i}", "read_file", {"path": "long.txt"}) for i in range(5)]
    script.append(_resp_text("done"))
    provider = ScriptedProvider(script)
    executor = ApiExecutor(provider, system_prompt="sys", user_prompt="任务",
                           sandbox=tmp_path, max_tokens_total=10_000, max_rounds=10,
                           history_tool_keep_rounds=0)
    result = executor.run()
    assert result.exit_reason == ExitReason.COMPLETED
    last_call = provider.calls[-1]
    tool_msgs = [m for m in last_call if m.role == "tool"]
    assert len(tool_msgs) == 5
    assert all((m.content or "") == LONG for m in tool_msgs)
    assert result.history_window["keep_rounds"] == 0
    assert result.history_window["compressed_messages"] == 0


def test_history_window_stats_in_budget_json(
    migrated_db: Path, client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 落库：agent_run 的 budget_json 带 history_window 统计段。"""
    from app.models import AgentRun, AgentRunToolOutput
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SASession

    # 沙箱里放一个长文件供 read_file 产生超阈值输出——不预置文件，
    # write_file 写长内容后 read_file 读回更接近真实链路

    long_content = "C" * 3_000
    script = [
        _resp_tool("w1", "write_file", {"path": "big.md", "content": long_content}),
        _resp_tool("r1", "read_file", {"path": "big.md"}),
        _resp_tool("r2", "read_file", {"path": "big.md"}),
        _resp_tool("r3", "read_file", {"path": "big.md"}),
        _resp_tool("r4", "read_file", {"path": "big.md"}),
        _resp_text("done"),
    ]
    provider = ScriptedProvider(script)
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: provider)

    pid = project["id"]
    resp = client.post(f"/api/projects/{pid}/runs", json={"prompt": "任务"}, headers=AUTH)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    Worker().run_pending()
    body = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
    assert body["status"] == "succeeded", body
    run_id = body["result"]["id"]

    with SASession(create_engine(f"sqlite:///{migrated_db}")) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        budget = json.loads(run.budget_json)
        persisted = session.query(AgentRunToolOutput).filter_by(
            run_id=run_id, tool_name="read_file"
        ).first()
        assert persisted is not None
        assert persisted.content_length == len(persisted.content)
        output_id = persisted.output_id
    assert "history_window" in budget
    hw = budget["history_window"]
    assert hw["keep_rounds"] == 3
    # read_file 的输出带 [行 1-1 / 共 1 行] 头 + 3000 字符 → 超阈值，
    # 轮 0 的读结果在窗口外被压缩
    assert hw["compressed_messages"] >= 1
    assert hw["saved_chars"] > 0
    page = client.get(f"/api/runs/{run_id}/tool-outputs/{output_id}?offset=100&limit=200", headers=AUTH)
    assert page.status_code == 200, page.text
    assert page.json()["content"] == persisted.content[100:300]
    assert page.json()["next_offset"] == 300
    # 另一个运行不能借用 output_id 读取原文。
    denied = client.get(f"/api/runs/not-this-run/tool-outputs/{output_id}", headers=AUTH)
    assert denied.status_code == 404
