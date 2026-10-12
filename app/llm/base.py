"""模型适配层：能力声明 + 消息类型 + Provider 协议（细化方案 主§11）。

约定：
- 适配层只做"协议翻译"，不做业务；上层（执行器/评分）以能力声明降级。
- 密钥一律从环境变量读取，不落盘、不进日志、不进 API 响应。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelError(Exception):
    """模型调用失败（网络/HTTP/协议解析）。附结构化上下文。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderNotConfigured(Exception):
    """模型提供商未配置（缺 base_url / model / 密钥）。"""


@dataclass(frozen=True)
class Capabilities:
    """提供商能力声明（主§11）。不支持的能力由上层降级或拒绝。"""

    tools: bool
    streaming: bool
    json_mode: bool
    max_context_tokens: int


# ---------- 消息与响应类型 ----------


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatMessage:
    """对话消息。assistant 可带 tool_calls；tool 消息须带 tool_call_id。"""

    role: str  # system | user | assistant | tool
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # 工具名（tool 消息）


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ModelResponse:
    """一次补全的结果。content 与 tool_calls 可同时为空（异常协议由适配层抛 ModelError）。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = field(default_factory=Usage)
    finish_reason: str | None = None


class ModelProvider(Protocol):
    """提供商协议：单提供商实现即可满足第一阶段。"""

    capabilities: Capabilities

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelResponse: ...
