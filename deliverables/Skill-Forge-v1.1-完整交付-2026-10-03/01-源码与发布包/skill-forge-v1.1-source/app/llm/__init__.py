"""模型适配层：能力声明、消息类型与 OpenAI 兼容提供商。"""
from app.llm.base import (
    Capabilities,
    ChatMessage,
    ModelError,
    ModelProvider,
    ModelResponse,
    ProviderNotConfigured,
    ToolCall,
    Usage,
)
from app.llm.openai_compat import OpenAICompatProvider, provider_from_env

__all__ = [
    "Capabilities",
    "ChatMessage",
    "ModelError",
    "ModelProvider",
    "ModelResponse",
    "ProviderNotConfigured",
    "ToolCall",
    "Usage",
    "OpenAICompatProvider",
    "provider_from_env",
]
