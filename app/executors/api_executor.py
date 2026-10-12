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
from app.tools import TOOL_SCHEMAS, ToolTimeout, execute_tool_output
from app.core.ulid import new_ulid

# v1.5 M3A：与 agent_run 处理器的 M2 收紧对齐（40→15）；生产路径
# 均显式传参，此值仅作库默认。依据：真实链路 completed 中位 22 步、
# 耗尽运行平均踱到 216k token 才止损。
MAX_ROUNDS_DEFAULT = 15

# v1.5 M3B：历史窗口化——工具输出随轮次全部重发进上下文是 token
# 膨胀主因（二次方增长）。最近 HISTORY_TOOL_KEEP_ROUNDS 轮的工具
# 输出逐字保留；更早的超过阈值的历史工具输出降档为头尾摘要
# （完整内容仍在 StepRecord 与沙箱磁盘，read_file 支持 offset/limit
# 分段重读，模型需要时可自行取回）。0 = 关闭窗口化。
HISTORY_TOOL_KEEP_ROUNDS_DEFAULT = 3
HISTORY_TOOL_COMPRESS_THRESHOLD = 600   # 低于此长度不值得压
HISTORY_TOOL_HEAD_CHARS = 200           # 摘要保留头部
HISTORY_TOOL_TAIL_CHARS = 200           # 摘要保留尾部


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
        history_tool_keep_rounds: int = HISTORY_TOOL_KEEP_ROUNDS_DEFAULT,
        on_step: Any = None,  # Callable[[StepRecord], None]；每步回调（进度事件用）
        persist_tool_output: Any = None,
        read_tool_output: Any = None,
    ) -> None:
        self.provider = provider
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.sandbox = sandbox
        self.budget = Budget(max_tokens_total=max_tokens_total, max_rounds=max_rounds)
        self.on_step = on_step
        self.persist_tool_output = persist_tool_output
        self.read_persisted_tool_output = read_tool_output
        # M3B：历史窗口化参数与状态
        self.history_tool_keep_rounds = max(history_tool_keep_rounds, 0)
        self._round_start_indices: list[int] = []   # 每轮首条消息（assistant）的 messages 索引
        self._compressed_indices: set[int] = set()   # 已降档的 tool 消息索引（幂等）
        self._history_saved_chars = 0
        self._tool_outputs: dict[str, str] = {}
        self._output_ids: dict[str, str] = {}
        self.request_calls = 0
        self.usage_missing_calls = 0
        self.unknown_usage_estimated_tokens = 0
        self.total_usage = Usage()

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
            "model_calls": self.request_calls,
            "usage_missing_calls": self.usage_missing_calls,
            "unknown_usage_estimated_tokens": self.unknown_usage_estimated_tokens,
            "accounted_tokens": self.budget.used_tokens + self.unknown_usage_estimated_tokens,
        }
        # M3B：历史窗口化的可观察性——本次执行累计压缩掉多少字符的历史
        kwargs.setdefault("history_window", {
            "keep_rounds": self.history_tool_keep_rounds,
            "compressed_messages": len(self._compressed_indices),
            "saved_chars": self._history_saved_chars,
        })
        return ExecutorResult(**kwargs)

    def _compress_stale_tool_outputs(self, messages: list[ChatMessage], round_index: int) -> None:
        """M3B：把窗口外旧轮的超长工具输出原地降档为头尾摘要。

        只改 messages 中 tool 消息的 content（tool_call_id 配对不变）；
        StepRecord 与沙箱磁盘保留完整内容，read_file 可分段重读。
        幂等：已降档索引不再处理。
        """
        if self.history_tool_keep_rounds == 0:
            return
        cutoff_round = round_index - self.history_tool_keep_rounds
        if cutoff_round <= 0 or not self._round_start_indices:
            return
        # 窗口外旧轮的消息区间：[round_start_indices[0], round_start_indices[cutoff_round])
        end = self._round_start_indices[cutoff_round]
        for i in range(self._round_start_indices[0], end):
            if i in self._compressed_indices:
                continue
            msg = messages[i]
            if msg.role != "tool" or msg.content is None:
                continue
            visible_original = msg.content
            original = self._tool_outputs.get(msg.tool_call_id or "", visible_original)
            if len(original) <= HISTORY_TOOL_COMPRESS_THRESHOLD:
                continue
            head = original[:HISTORY_TOOL_HEAD_CHARS]
            tail = original[-HISTORY_TOOL_TAIL_CHARS:]
            msg.content = (
                f"{head}\n……（历史工具输出已压缩：原始 {len(original)} 字符，"
                f"中段省略；如需恢复原始工具结果，调用 read_tool_output，"
                f"output_id={self._output_ids.get(msg.tool_call_id, msg.tool_call_id)!r}，"
                f"可用 offset/limit 分段读取）……\n{tail}"
            )
            self._compressed_indices.add(i)
            self._history_saved_chars += len(visible_original) - len(msg.content)

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

            # M3B：模型调用前把窗口外旧轮工具输出降档（本次请求不再全量重发）
            self._compress_stale_tool_outputs(messages, round_index)

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
            request_token_reservation = min(4_096, max(1, self.budget.remaining_tokens()))
            self.request_calls += 1
            try:
                resp: ModelResponse = self.provider.complete(
                    messages,
                    tools=tools,
                    max_tokens=request_token_reservation,
                )
            except ModelError as exc:
                self.usage_missing_calls += 1
                self.unknown_usage_estimated_tokens += request_token_reservation
                return self._result(
                    exit_reason=ExitReason.MODEL_FAILED,
                    final_message=final_message,
                    steps=steps,
                    total_usage=total,
                    error_detail=str(exc),
                )
            except Exception:
                # A request was sent but no provider usage was returned. Preserve
                # earlier measured usage and account conservatively for this call.
                self.usage_missing_calls += 1
                self.unknown_usage_estimated_tokens += request_token_reservation
                raise
            duration_ms = int((time.monotonic() - started) * 1000)
            self.budget.consume(resp.usage)
            total = Usage(
                prompt_tokens=total.prompt_tokens + resp.usage.prompt_tokens,
                completion_tokens=total.completion_tokens + resp.usage.completion_tokens,
            )
            self.total_usage = total

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
                    if tc.name == "read_tool_output":
                        output_id = str(tc.arguments.get("output_id") or tc.arguments.get("call_id") or "")
                        call_id = next((key for key, value in self._output_ids.items()
                                        if value == output_id), output_id)
                        original = self._tool_outputs.get(call_id)
                        if original is None and self.read_persisted_tool_output is not None:
                            original = self.read_persisted_tool_output(output_id)
                        if original is None:
                            output = json.dumps({
                                "error": "tool_output_not_found",
                                "message": "该 output_id 不存在或不属于本次运行",
                            }, ensure_ascii=False)
                        else:
                            try:
                                offset = max(0, int(tc.arguments.get("offset") or 0))
                                raw_limit = tc.arguments.get("limit")
                                limit = 4_000 if raw_limit is None else int(raw_limit)
                                if limit < 1 or limit > 4_000:
                                    raise ValueError("limit 必须在 1 到 4000 之间")
                            except (TypeError, ValueError):
                                output = json.dumps({"error": "bad_offset_limit"})
                            else:
                                output = original[offset:offset + limit]
                                if offset + limit < len(original):
                                    output += (
                                        f"\n……（已返回字符 {offset}-"
                                        f"{offset + len(original[offset:offset + limit])} / "
                                        f"{len(original)}，继续读取请增加 offset）"
                                    )
                    else:
                        tool_result = execute_tool_output(self.sandbox, tc.name, tc.arguments)
                        output = tool_result.visible
                        ref = new_ulid()
                        self._output_ids[tc.id] = ref
                        self._tool_outputs[tc.id] = tool_result.full
                        tool_step.output = tool_result.full
                        tool_step.output_id = ref
                        tool_step.output_length = len(tool_result.full)
                        if self.persist_tool_output is not None:
                            self.persist_tool_output(ref, tc.id, tc.name, tool_result.full)
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
                if tc.name == "read_tool_output":
                    tool_step.output = output
                tool_step.duration_ms = int((time.monotonic() - started) * 1000)
                steps.append(tool_step)
                self._emit(tool_step)
                messages.append(ChatMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=output))

            final_message = resp.content
            self._round_start_indices.append(len(messages) - len(resp.tool_calls) - 1)
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
