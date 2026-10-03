"""工具注册表：read_file / write_file / run_script（细化方案 主§11、§2）。

约定：
- 工具只在一个沙箱目录内操作（版本快照的临时展开目录），路径穿越一律拒绝。
- run_script 只运行白名单扩展名，带超时与输出截断。
- 工具返回字符串（进模型上下文）；错误返回 JSON 错误串，不抛异常打断循环。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from app.services.skill_files import FileValidationError, normalize_relpath

MAX_TOOL_OUTPUT_CHARS = 20_000      # 单次工具输出截断上限
SCRIPT_TIMEOUT_SECONDS = 60         # 脚本执行超时
SCRIPT_MAX_OUTPUT_BYTES = 1_000_000  # 脚本 stdout+stderr 原始字节上限

SCRIPT_EXTS = frozenset({".py", ".sh", ".ps1"})


class ToolValidationError(Exception):
    """工具入参非法（路径穿越等）——记录为轨迹而非打断循环。"""


class ToolTimeout(Exception):
    """脚本执行超时。"""


# ---------- schema 声明（进模型的工具定义） ----------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取沙箱工作目录中的一个文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 SKILL.md"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "在沙箱工作目录中写入（覆盖）一个文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "在沙箱工作目录中执行一个脚本（.py/.sh/.ps1），返回 stdout/stderr。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "脚本相对路径"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "传给脚本的命令行参数（可选）",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


# ---------- 路径解析 ----------


def resolve_sandbox_path(sandbox: Path, raw: str) -> Path:
    """在沙箱目录内解析相对路径；穿越即抛 ToolValidationError。"""
    relpath = normalize_relpath(raw)  # 复用 S3 的全部穿越检查
    target = sandbox.joinpath(*relpath.parts)
    # F05：is_relative_to 做层级判断，避免字符串前缀把 /sandbox-evil 误判为 /sandbox 内
    if not target.resolve().is_relative_to(sandbox.resolve()):
        raise ToolValidationError(f"路径越出沙箱: {raw!r}")
    return target


# ---------- 工具实现 ----------


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n…（已截断，原始 {len(text)} 字符）"


def read_file(sandbox: Path, arguments: dict[str, Any]) -> str:
    try:
        path = resolve_sandbox_path(sandbox, str(arguments.get("path") or ""))
    except (ToolValidationError, FileValidationError) as exc:
        return json.dumps({"error": "path_rejected", "message": str(exc)}, ensure_ascii=False)
    if not path.is_file():
        return json.dumps({"error": "file_not_found", "path": arguments.get("path")}, ensure_ascii=False)
    try:
        return _truncate(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return json.dumps({"error": "read_failed", "message": str(exc)}, ensure_ascii=False)


def write_file(sandbox: Path, arguments: dict[str, Any]) -> str:
    try:
        path = resolve_sandbox_path(sandbox, str(arguments.get("path") or ""))
    except (ToolValidationError, FileValidationError) as exc:
        return json.dumps({"error": "path_rejected", "message": str(exc)}, ensure_ascii=False)
    content = str(arguments.get("content") if arguments.get("content") is not None else "")
    if len(content.encode("utf-8")) > 1_000_000:
        return json.dumps({"error": "content_too_large"}, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return json.dumps({"ok": True, "path": arguments.get("path"), "bytes": len(content.encode("utf-8"))}, ensure_ascii=False)


def _run_py(path: Path, args: list[str], sandbox: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 参数受控
        [sys.executable, str(path), *args],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_SECONDS,
    )

def _run_sh(path: Path, args: list[str], sandbox: Path) -> subprocess.CompletedProcess[str]:
    import shutil
    bash = shutil.which("bash") or "bash"
    return subprocess.run(  # noqa: S603
        [bash, str(path), *args],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_SECONDS,
    )


def _run_ps1(path: Path, args: list[str], sandbox: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(path), *args],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_SECONDS,
    )


def run_script(sandbox: Path, arguments: dict[str, Any]) -> str:
    try:
        path = resolve_sandbox_path(sandbox, str(arguments.get("path") or ""))
    except (ToolValidationError, FileValidationError) as exc:
        return json.dumps({"error": "path_rejected", "message": str(exc)}, ensure_ascii=False)
    if not path.is_file():
        return json.dumps({"error": "file_not_found", "path": arguments.get("path")}, ensure_ascii=False)
    if path.suffix.lower() not in SCRIPT_EXTS:
        return json.dumps({"error": "script_type_not_allowed", "suffix": path.suffix}, ensure_ascii=False)
    args = [str(a) for a in (arguments.get("args") or [])]
    # F01：执行用绝对路径（resolve 后），避免子进程 cwd=sandbox 时相对路径被再次拼接
    run_path = path.resolve()
    runner: Callable[..., subprocess.CompletedProcess[str]] = {
        ".py": _run_py,
        ".sh": _run_sh,
        ".ps1": _run_ps1,
    }[path.suffix.lower()]
    try:
        proc = runner(run_path, args, sandbox)
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeout(f"脚本超时（>{SCRIPT_TIMEOUT_SECONDS}s）: {arguments.get('path')}") from exc
    stdout = (proc.stdout or "")[:SCRIPT_MAX_OUTPUT_BYTES]
    stderr = (proc.stderr or "")[:SCRIPT_MAX_OUTPUT_BYTES]
    return _truncate(json.dumps({
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }, ensure_ascii=False))


# ---------- 注册表 ----------

ToolFn = Callable[[Path, dict[str, Any]], str]

TOOLS: dict[str, ToolFn] = {
    "read_file": read_file,
    "write_file": write_file,
    "run_script": run_script,
}


def execute_tool(sandbox: Path, name: str, arguments: dict[str, Any]) -> str:
    """按名执行工具；未知工具返回错误 JSON（不抛异常打断循环）。"""
    fn = TOOLS.get(name)
    if fn is None:
        return json.dumps({"error": "unknown_tool", "name": name}, ensure_ascii=False)
    return fn(sandbox, arguments)
