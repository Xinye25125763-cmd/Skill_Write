"""宿主适配层：导入即注册内置适配器（审计 C10）。

`app.platforms` 包导入时完成 CodexAdapter 注册；此前 __init__ 为空，
只有直接 import app.platforms.base 才会触发注册，导入包本身不生效。
"""
from app.platforms.base import register_adapter


def _register_builtin() -> None:
    from app.platforms.codex import CodexAdapter

    register_adapter(CodexAdapter())


_register_builtin()

from app.platforms.base import get_adapter, HostAdapter  # noqa: E402,F401

__all__ = ["HostAdapter", "register_adapter", "get_adapter"]