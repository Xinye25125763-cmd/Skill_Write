"""API 执行器 v0：自实现工具循环（细化方案 S5 完成定义）。

循环：模型补全 → 若带 tool_calls 则执行工具并把结果回填 → 直至模型不再调用工具
（COMPLETED）或触发退出条件（BUDGET_EXCEEDED / TOOL_TIMEOUT / MODEL_FAILED）。

预算（主§9）：
- max_tokens_total：全循环 token 预算上限
- max_rounds：工具循环轮次上限
每轮之间检查协作取消。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.executors.base import ExitReason, ExecutorResult, StepRecord
from app.llm.base import ChatMessage, ModelError, ModelProvider, ModelResponse, Usage
from app.tools import TOOL_SCHEMAS, ToolTimeout, execute_tool

MAX_ROUNDS_DEFAULT = 40


class Budget:
    """循环预算：token 与轮次双上限，超限即耗尽。"""

    def __init__(self, *, max_tokens_total: int, max_rounds: int = MAX_ROUNDS_DEFAULT) -> None:
        self.max_tokens_total = max_tokens_total
        self.max_rounds = max_rounds
        self.used_tokens = 0

    def consume(self, usage: Usage) -> None:
        self.used_tokens += usage.total_tokens

    @property
    def exhausted(self) -> bool:
        return self.used_tokens >= self.max_tokens_total

    def rounds_exhausted_for(self, round_index: int) -> bool:
        return round_index >= self.max_rounds

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens_total - self.used_tokens)


class ApiExecutor:
    """自实现工具循环执行器。"""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        system_prompt: str,
        user_prompt: str,
        sandbox: Path,
        max_tokens_total: int = 200_000,
        max_rounds: int = MAX_ROUNDS_DEFAULT,
        on_step: Any = None,  # Callable[[StepRecord], None]；每步回调（进度事件用）
    ) -> None:
        self.provider = provider
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.sandbox = sandbox
        self.budget = Budget(max_tokens_total=max_tokens_total, max_rounds=max_rounds)
        self.on_step = on_step

    def _emit(self, step: StepRecord) -> None:
        if self.on_step is not None:
            self.on_step(step)

    def _result(self, **kwargs: Any) -> ExecutorResult:
        """Every exit retains artifacts and separates completion from budget compliance."""
        if "sandbox_files" not in kwargs:
            kwargs["sandbox_files"] = self._collect_sandbox_files()
        kwargs["budget"] = {
            "limit_tokens": self.budget.max_tokens_total,
            "used_tokens": self.budget.used_tokens,
            "over_limit": self.budget.used_tokens > self.budget.max_tokens_total,
            "overrun_tokens": max(0, self.budget.used_tokens - self.budget.max_tokens_total),
        }
        return ExecutorResult(**kwargs)

    def run(self, *, is_cancelled: Any = None) -> ExecutorResult:
        """执行循环。is_cancelled 为无参谓词，轮次边界检查。"""
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(role="user", content=self.user_prompt),
        ]
        steps: list[StepRecord] = []
        total = Usage()
        final_message: str | None = None
        tools = TOOL_SCHEMAS if self.provider.capabilities.tools else None

        round_index = 0
        while True:
            # 轮次边界：协作取消
            if is_cancelled is not None and is_cancelled():
                return self._result(
                    exit_reason=ExitReason.CANCELLED,
                    final_message=final_message,
                    steps=steps,
                    total_usage=total,
                )

            # 轮次预算
            if self.budget.rounds_exhausted_for(round_index):
                return self._result(
                    exit_reason=ExitReason.BUDGET_EXCEEDED,
                    final_message=final_message,
                    steps=steps,
                    total_usage=total,
                    error_detail=f"轮次预算耗尽（>{self.budget.max_rounds} 轮）",
                )
            # token 预算（调用前检查，防止明知超限仍发请求）
            if self.budget.exhausted:
                return self._result(
                    exit_reason=ExitReason.BUDGET_EXCEEDED,
                    final_message=final_message,
                    steps=steps,
                    total_usage=total,
                    error_detail=f"token 预算耗尽（{self.budget.used_tokens}/{self.budget.max_tokens_total}）",
                )

            # 模型调用
            started = time.monotonic()
            try:
                resp: ModelResponse = self.provider.complete(
                    messages,
                    tools=tools,
                    max_tokens=min(4_096, max(1, self.budget.remaining_tokens())),
                )
            except ModelError as exc:
                return self._result(
                    exit_reason=ExitReason.MODEL_FAILED,
                    final_message=final_message,
                    steps=steps,
                    total_usage=total,
                    error_detail=str(exc),
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            self.budget.consume(resp.usage)
            total = Usage(
                prompt_tokens=total.prompt_tokens + resp.usage.prompt_tokens,
                completion_tokens=total.completion_tokens + resp.usage.completion_tokens,
            )

            model_step = StepRecord(
                kind="model",
                content=resp.content,
                usage=resp.usage,
                duration_ms=duration_ms,
            )
            steps.append(model_step)
            self._emit(model_step)

            # Cancellation received during a model call must fence this response's tools.
            if is_cancelled is not None and is_cancelled():
                return self._result(exit_reason=ExitReason.CANCELLED,
                                    final_message=resp.content, steps=steps, total_usage=total)
            if resp.tool_calls and self.budget.exhausted:
                return self._result(
                    exit_reason=ExitReason.BUDGET_EXCEEDED,
                    final_message=resp.content, steps=steps, total_usage=total,
                    error_detail="token 预算耗尽，未执行本轮工具调用",
                )

            # 消耗后区分输出完成状态与预算；正常 stop 允许保留 completed，
            # 同时 budget.over_limit 明确记录超额，不把截断内容视为完成。
            if not resp.tool_calls and self.budget.exhausted and resp.finish_reason != "stop":
                return self._result(
                    exit_reason=ExitReason.BUDGET_EXCEEDED,
                    final_message=resp.content,
                    steps=steps,
                    total_usage=total,
                    error_detail=f"token 预算耗尽（{self.budget.used_tokens}/{self.budget.max_tokens_total}，finish_reason={resp.finish_reason!r}）",
                    sandbox_files=self._collect_sandbox_files(),
                )

            if not resp.tool_calls:
                # F07：区分正常结束与异常结束。
                # - finish_reason=length：输出被截断 → TRUNCATED（不是 completed）
                # - content_filter 等：内容被拒 → MODEL_FAILED（带证据）
                # 只有 stop（或提供商未返回 finish_reason）才视为任务完成。
                if resp.finish_reason == "length":
                    return self._result(
                        exit_reason=ExitReason.TRUNCATED,
                        final_message=resp.content,
                        steps=steps,
                        total_usage=total,
                        error_detail="模型输出被截断（finish_reason=length）",
                        sandbox_files=self._collect_sandbox_files(),
                    )
                if resp.finish_reason not in (None, "stop", "tool_calls"):
                    return self._result(
                        exit_reason=ExitReason.MODEL_FAILED,
                        final_message=resp.content,
                        steps=steps,
                        total_usage=total,
                        error_detail=f"模型异常结束（finish_reason={resp.finish_reason!r}）",
                        sandbox_files=self._collect_sandbox_files(),
                    )
                # 模型不再调用工具 → 任务完成
                return self._result(
                    exit_reason=ExitReason.COMPLETED,
                    final_message=resp.content,
                    steps=steps,
                    total_usage=total,
                    sandbox_files=self._collect_sandbox_files(),
                )

            # 记录 assistant 的工具调用意图，再逐个执行并回填
            messages.append(ChatMessage(role="assistant", content=resp.content, tool_calls=resp.tool_calls))
            for tc in resp.tool_calls:
                if is_cancelled is not None and is_cancelled():
                    return self._result(exit_reason=ExitReason.CANCELLED,
                                        final_message=resp.content, steps=steps, total_usage=total)
                tool_step = StepRecord(
                    kind="tool",
                    name=tc.name,
                    arguments=tc.arguments if isinstance(tc.arguments, dict) else None,
                )
                started = time.monotonic()
                # F08①：工具参数结构校验——非对象（如 JSON 数组）不执行，
                # 错误 JSON 回填模型上下文（模型可自纠），不打断循环
                if not isinstance(tc.arguments, dict):
                    output = json.dumps(
                        {"error": "invalid_arguments", "message": "工具参数必须是对象（JSON object），当前不是"},
                        ensure_ascii=False,
                    )
                    tool_step.output = output
                    tool_step.duration_ms = int((time.monotonic() - started) * 1000)
                    steps.append(tool_step)
                    self._emit(tool_step)
                    messages.append(ChatMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=output))
                    continue
                try:
                    output = execute_tool(self.sandbox, tc.name, tc.arguments)
                except ToolTimeout as exc:
                    tool_step.output = f'{{"error": "tool_timeout", "message": "{exc}"}}'
                    tool_step.duration_ms = int((time.monotonic() - started) * 1000)
                    steps.append(tool_step)
                    self._emit(tool_step)
                    return self._result(
                        exit_reason=ExitReason.TOOL_TIMEOUT,
                        final_message=final_message,
                        steps=steps,
                        total_usage=total,
                        error_detail=str(exc),
                    )
                except Exception as exc:  # noqa: BLE001 F08②：预期操作错误兑底
                    # 工具层未及捕获的意外异常（如写到已存在目录路径）
                    # 也结构化回填模型，不打断循环、不逃逸到 Worker
                    output = json.dumps(
                        {"error": "tool_failed", "tool": tc.name, "message": f"{type(exc).__name__}: {exc}"},
                        ensure_ascii=False,
                    )
                tool_step.output = output
                tool_step.duration_ms = int((time.monotonic() - started) * 1000)
                steps.append(tool_step)
                self._emit(tool_step)
                messages.append(ChatMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=output))

            final_message = resp.content
            round_index += 1

    def _collect_sandbox_files(self) -> dict:
        """F09：收集沙箱最终文件——清单（含二进制）与文本内容分离。

        返回 {"files": [{path, size, text}], "text_contents": {path: content}}：
        - files：全量文件清单（二进制也在内，file_exists 据此判定）；
        - text_contents：仅 UTF-8 可解码文件（file_contains / 前端展示据此）。
        """
        files: list[dict] = []
        text_contents: dict[str, str] = {}
        for p in sorted(self.sandbox.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.sandbox).as_posix()
                size = p.stat().st_size
                try:
                    text_contents[rel] = p.read_text(encoding="utf-8")
                    files.append({"path": rel, "size": size, "text": True})
                except UnicodeDecodeError:
                    files.append({"path": rel, "size": size, "text": False})
        return {"files": files, "text_contents": text_contents}
