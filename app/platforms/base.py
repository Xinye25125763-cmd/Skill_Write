"""宿主适配层基类与注册表（工程计划 §5 T18）。

约定：
- capabilities() 只报实际验证过的能力——注册 ≠ 支持（其他平台不自动标支持）；
- validate_delivery() 输出 CheckReport（check_id 前缀 host:），
  证据必须来自宿主侧真实执行记录，API 注入结果不能冒充原生触发；
- 宿主不可用 → blocked 证据 + not_run/unsupported，绝不能 passed。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.generation.contracts import CheckReport, DeliveryReceipt


class HostAdapter(ABC):
    """宿主适配器：能力声明 + 交付验证。"""

    #: 是否完成过真实宿主验收（人工或自动证据），只有验证过的才报 True
    VERIFIED: bool = False
    #: 是否支持原生触发执行（真实宿主进程判定，非 API 注入）
    NATIVE_TRIGGER: bool = False

    platform: str = "unknown"

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """能力声明：platform/verified/native_trigger + 平台特定项。"""

    @abstractmethod
    def validate_delivery(
        self, receipt: DeliveryReceipt, cases: list[dict[str, Any]]
    ) -> CheckReport:
        """在真实宿主上验证交付：正/负触发、资源加载、脚本条件与输出。

        cases 元素：{case_id, kind: trigger|execution, prompt, expect}。
        expect 取值：hit / miss / output_contains:<子串>。
        """


class _UnverifiedAdapter(HostAdapter):
    """未验证平台的占位适配器：不自动标支持（验收 5）。"""

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def capabilities(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "verified": False,
            "native_trigger": False,
            "note": "未适配：注册不代表支持，需完成真实宿主验收后才标 verified",
        }

    def validate_delivery(
        self, receipt: DeliveryReceipt, cases: list[dict[str, Any]]
    ) -> CheckReport:
        from app.generation.contracts import CheckResult, CheckStatus

        checks = [
            CheckResult(
                check_id=f"host:{self.platform}:available",
                status=CheckStatus.UNSUPPORTED,
                evidence=f"blocked：宿主 {self.platform} 未适配，无法自动验证",
            )
        ]
        for case in cases:
            checks.append(CheckResult(
                check_id=f"host:{self.platform}:case:{case.get('case_id', '')}",
                status=CheckStatus.NOT_RUN,
                evidence="宿主未适配（unsupported），不运行",
            ))
        return CheckReport(checks=checks)


_REGISTRY: dict[str, HostAdapter] = {}


def register_adapter(adapter: HostAdapter) -> None:
    """注册适配器（同平台后注册覆盖前者）。"""
    _REGISTRY[adapter.platform] = adapter


def get_adapter(platform: str) -> HostAdapter | None:
    """按平台取适配器；未注册平台返回占位适配器（不自动标支持）。"""
    if platform in _REGISTRY:
        return _REGISTRY[platform]
    if not platform:
        return None
    return _UnverifiedAdapter(platform)


def _register_builtin() -> None:
    from app.platforms.codex import CodexAdapter

    register_adapter(CodexAdapter())


_register_builtin()
