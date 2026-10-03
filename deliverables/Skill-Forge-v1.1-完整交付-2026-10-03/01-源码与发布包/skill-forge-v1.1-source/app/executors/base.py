"""执行器接口 + 退出原因枚举（细化方案 §2 executors/base.py）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.llm.base import Usage


class ExitReason(str, Enum):
    """循环退出原因（主§11：预算/超时/模型失败均须可区分）。"""

    COMPLETED = "completed"            # 模型声明任务完成（不再调用工具）
    TRUNCATED = "truncated"            # F07：模型输出被截断（finish_reason=length），不视为完成
    BUDGET_EXCEEDED = "budget_exceeded"  # 预算（tokens 或轮次）耗尽
    TOOL_TIMEOUT = "tool_timeout"      # 工具执行超时
    MODEL_FAILED = "model_failed"      # 模型调用失败
    CANCELLED = "cancelled"            # 用户取消（协作取消）


@dataclass
class StepRecord:
    """轨迹中的一步。"""

    kind: str                    # model | tool
    name: str | None = None      # 工具名 / None
    arguments: dict[str, Any] | None = None
    content: str | None = None
    output: str | None = None    # 工具输出 / 模型内容
    usage: Usage | None = None
    duration_ms: int | None = None


@dataclass
class ExecutorResult:
    """执行器返回值：退出原因 + 轨迹 + 用量。"""

    exit_reason: ExitReason
    final_message: str | None = None
    steps: list[StepRecord] = field(default_factory=list)
    total_usage: Usage = field(default_factory=Usage)
    error_detail: str | None = None
    # F09：{"files": [{path, size, text}], "text_contents": {path: content}}
    # files 含二进制全量清单；text_contents 仅 UTF-8 文本内容
    sandbox_files: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.exit_reason == ExitReason.COMPLETED
