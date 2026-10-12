"""Per-provider-call usage tracking, including calls that fail without usage."""
from __future__ import annotations

from typing import Any


class ModelTokenBudgetExceeded(Exception):
    """No output allowance remains for another provider request."""


class UsageTrackingProvider:
    """Transparent provider wrapper that retains measured and missing-call counts."""

    def __init__(
        self, provider: Any, *, max_tokens_total: int | None = None,
        used_tokens: int = 0,
    ) -> None:
        self.provider = provider
        self.capabilities = provider.capabilities
        self.max_tokens_total = max_tokens_total
        self.used_tokens = max(0, int(used_tokens))
        self.model_calls = 0
        self.usage_calls = 0
        self.usage_missing_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.unknown_usage_estimated_tokens = 0

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN201
        if self.max_tokens_total is not None:
            remaining = self.max_tokens_total - self.used_tokens
            if remaining <= 0:
                raise ModelTokenBudgetExceeded(
                    f"全流程 token 预算已耗尽（{self.used_tokens}/{self.max_tokens_total}）"
                )
            requested = kwargs.get("max_tokens")
            kwargs["max_tokens"] = min(int(requested), remaining) if requested else remaining
        self.model_calls += 1
        try:
            response = self.provider.complete(messages, **kwargs)
        except Exception:
            self.usage_missing_calls += 1
            estimate = max(0, int(kwargs.get("max_tokens") or 0))
            self.unknown_usage_estimated_tokens += estimate
            self.used_tokens += estimate
            raise
        usage = getattr(response, "usage", None)
        if usage is None:
            self.usage_missing_calls += 1
            estimate = max(0, int(kwargs.get("max_tokens") or 0))
            self.unknown_usage_estimated_tokens += estimate
            self.used_tokens += estimate
        else:
            self.usage_calls += 1
            self.prompt_tokens += int(usage.prompt_tokens or 0)
            self.completion_tokens += int(usage.completion_tokens or 0)
            self.used_tokens += int(usage.prompt_tokens or 0) + int(usage.completion_tokens or 0)
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
            "limit_tokens": self.max_tokens_total,
            "accounted_tokens": self.used_tokens,
            "over_limit": (self.max_tokens_total is not None
                           and self.used_tokens > self.max_tokens_total),
            "overrun_tokens": (max(0, self.used_tokens - self.max_tokens_total)
                               if self.max_tokens_total is not None else 0),
        }
