"""OpenAI 兼容 HTTP 适配器（单提供商，主§11 能力声明模型）。

适用于一切 /v1/chat/completions 兼容端点（OpenAI / DeepSeek / 本地 vLLM 等）。
配置只走环境变量（密钥绝不进 config.yaml / git）：
  SKILLFORGE_LLM_BASE_URL  端点地址（如 https://api.deepseek.com）
  SKILLFORGE_LLM_MODEL     模型名（如 deepseek-chat）
  SKILLFORGE_LLM_API_KEY   密钥（本地无鉴权端点可不设）
本地快速启动见仓库根 run-deepseek.ps1（gitignore，key 不入库）。
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.llm.base import (
    Capabilities,
    ChatMessage,
    ModelError,
    ModelResponse,
    ProviderNotConfigured,
    ToolCall,
    Usage,
)

DEFAULT_TIMEOUT_SECONDS = 120.0


class OpenAICompatProvider:
    """OpenAI Chat Completions 兼容适配器。"""

    def __init__(self, *, base_url: str, model: str, api_key: str | None) -> None:
        if not base_url or not model:
            raise ProviderNotConfigured("base_url 与 model 均须配置")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.capabilities = Capabilities(
            tools=True,
            streaming=False,  # v0 不用流式：SSE 进度由事件层承担
            json_mode=True,
            max_context_tokens=128_000,
        )

    # ---------- 请求组装 ----------

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _serialize_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.content or "",
                })
            elif m.role == "assistant" and m.tool_calls:
                out.append({
                    "role": "assistant",
                    "content": m.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in m.tool_calls
                    ],
                })
            else:
                out.append({"role": m.role, "content": m.content or ""})
        return out

    # ---------- 调用 ----------

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelResponse:
        if not self.capabilities.tools and tools:
            raise ModelError("该提供商不支持工具调用")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(messages),
        }
        if tools:
            body["tools"] = tools
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature

        url = f"{self.base_url}/chat/completions"
        try:
            resp = httpx.post(
                url,
                json=body,
                headers=self._headers(),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ModelError(f"模型网络错误: {exc}") from exc

        if resp.status_code >= 400:
            raise ModelError(
                f"模型 HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError) as exc:
            raise ModelError(f"模型响应协议异常: {resp.text[:500]}") from exc

        message = choice.get("message") or {}
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except ValueError:
                args = {"_raw": args_raw}
            tool_calls.append(ToolCall(id=tc.get("id") or "", name=fn.get("name") or "", arguments=args))

        u = data.get("usage")
        usage = None
        if isinstance(u, dict) and (
            "prompt_tokens" in u or "completion_tokens" in u
        ):
            usage = Usage(
                prompt_tokens=int(u.get("prompt_tokens") or 0),
                completion_tokens=int(u.get("completion_tokens") or 0),
            )
        return ModelResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.get("finish_reason"),
        )


def provider_from_env() -> OpenAICompatProvider:
    """从环境变量构造提供商；未配置时抛 ProviderNotConfigured。"""
    base_url = os.environ.get("SKILLFORGE_LLM_BASE_URL", "")
    model = os.environ.get("SKILLFORGE_LLM_MODEL", "")
    api_key = os.environ.get("SKILLFORGE_LLM_API_KEY")
    if not base_url or not model:
        raise ProviderNotConfigured(
            "未配置模型提供商：请设置 SKILLFORGE_LLM_BASE_URL 与 SKILLFORGE_LLM_MODEL"
        )
    return OpenAICompatProvider(base_url=base_url, model=model, api_key=api_key)


def judge_provider_from_env() -> OpenAICompatProvider:
    """评审侧专用提供商（v1.5 M5 分级模型）。

    优先读 SKILLFORGE_JUDGE_LLM_BASE_URL / _MODEL / _API_KEY——盲评评审、
    触发验证判定这类"裁判"调用可配置更便宜或更强的模型；任一必填项
    缺省即整体回落主模型（provider_from_env），不做半套配置的拼接。
    """
    base_url = os.environ.get("SKILLFORGE_JUDGE_LLM_BASE_URL", "")
    model = os.environ.get("SKILLFORGE_JUDGE_LLM_MODEL", "")
    api_key = os.environ.get("SKILLFORGE_JUDGE_LLM_API_KEY")
    if base_url and model:
        return OpenAICompatProvider(base_url=base_url, model=model, api_key=api_key)
    return provider_from_env()
