from __future__ import annotations

from typing import Any

from app.llm import ChatMessage, Usage
from app.llm.openai_compat import OpenAICompatProvider


class _Response:
    status_code = 200
    text = "ok"

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def json(self) -> dict[str, Any]:
        return self.body


def test_openai_compatible_provider_preserves_missing_usage(
    monkeypatch,
) -> None:
    import app.llm.openai_compat as module

    monkeypatch.setattr(module.httpx, "post", lambda *_a, **_k: _Response({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    }))
    provider = OpenAICompatProvider(
        base_url="https://example.invalid/v1", model="sample", api_key=None,
    )
    response = provider.complete([ChatMessage(role="user", content="hello")])
    assert response.content == "ok"
    assert response.usage is None


def test_openai_compatible_provider_parses_reported_usage(monkeypatch) -> None:
    import app.llm.openai_compat as module

    monkeypatch.setattr(module.httpx, "post", lambda *_a, **_k: _Response({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }))
    provider = OpenAICompatProvider(
        base_url="https://example.invalid/v1", model="sample", api_key=None,
    )
    response = provider.complete([ChatMessage(role="user", content="hello")])
    assert response.usage == Usage(prompt_tokens=12, completion_tokens=4)
