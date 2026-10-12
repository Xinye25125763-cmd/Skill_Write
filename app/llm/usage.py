"""Per-provider-call usage tracking, including calls that fail without usage."""
from __future__ import annotations

from typing import Any


class UsageTrackingProvider:
    """Transparent provider wrapper that retains measured and missing-call counts."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.capabilities = provider.capabilities
        self.model_calls = 0
        self.usage_calls = 0
        self.usage_missing_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.unknown_usage_estimated_tokens = 0

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN201
        self.model_calls += 1
        try:
            response = self.provider.complete(messages, **kwargs)
        except Exception:
            self.usage_missing_calls += 1
            self.unknown_usage_estimated_tokens += max(0, int(kwargs.get("max_tokens") or 0))
            raise
        usage = getattr(response, "usage", None)
        if usage is None:
            self.usage_missing_calls += 1
            self.unknown_usage_estimated_tokens += max(0, int(kwargs.get("max_tokens") or 0))
        else:
            self.usage_calls += 1
            self.prompt_tokens += int(usage.prompt_tokens or 0)
            self.completion_tokens += int(usage.completion_tokens or 0)
        return response

    def meta(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "usage": ({"prompt_tokens": self.prompt_tokens,
                       "completion_tokens": self.completion_tokens}
                      if self.usage_missing_calls == 0 and self.usage_calls else None),
            "usage_observed": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            },
            "usage_calls": self.usage_calls,
            "usage_missing_calls": self.usage_missing_calls,
            "unknown_usage_estimated_tokens": self.unknown_usage_estimated_tokens,
        }
