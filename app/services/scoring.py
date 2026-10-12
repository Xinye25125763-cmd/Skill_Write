"""程序断言评分（S6 最小版）。

断言类型（v0）：
    file_exists    {path}
    file_contains  {path, contains}
    text_contains  {contains}          # 针对 final_message
    exit_reason_is {value}             # 针对执行退出原因

约定：
- 单条断言不合法（缺字段/未知类型）→ 该断言记 failed（detail 说明），
  不中断其余断言；
- 断言列表整体不是合法 JSON 数组 → ScoringError（评分失败，与执行失败分离）。

F09：sandbox_files 参数为 {"files": [{path,size,text}], "text_contents": {path: content}}；
file_exists 查全量清单（含二进制），file_contains 仅查文本内容图。
"""
from __future__ import annotations

from typing import Any


class ScoringError(Exception):
    """评分步骤本身失败（断言结构非法），区别于断言不通过。"""


def _manifest_paths(sandbox_files: Any) -> set[str]:
    """从 sandbox_files 提取全量文件路径集合（兼容旧版纯 path→content 映射）。"""
    if isinstance(sandbox_files, dict) and isinstance(sandbox_files.get("files"), list):
        return {str(f.get("path")) for f in sandbox_files["files"] if isinstance(f, dict)}
    if isinstance(sandbox_files, dict):
        return set(sandbox_files.keys())  # 旧格式：path → content
    return set()


def _text_contents(sandbox_files: Any) -> dict[str, str]:
    """提取文本内容映射（兼容旧版纯 path→content 映射）。"""
    if isinstance(sandbox_files, dict):
        tc = sandbox_files.get("text_contents")
        if isinstance(tc, dict):
            return {str(k): str(v) for k, v in tc.items()}
        if "files" not in sandbox_files:
            return {str(k): str(v) for k, v in sandbox_files.items()}  # 旧格式
    return {}


def _describe(a: dict[str, Any]) -> str:
    t = a.get("type")
    if t == "file_exists":
        return f"文件存在: {a.get('path')}"
    if t == "file_contains":
        return f"文件 {a.get('path')} 含 {a.get('contains')!r}"
    if t == "text_contains":
        return f"答复含 {a.get('contains')!r}"
    if t == "exit_reason_is":
        return f"退出原因 = {a.get('value')}"
    return f"断言({t})"


def score_assertions(
    assertions: Any,
    *,
    sandbox_files: Any,
    final_message: str | None,
    exit_reason: str | None,
) -> list[dict[str, Any]]:
    """逐条评分；返回 [{type, description, passed, detail}]。

    断言列表整体非法（非列表/元素非对象）→ ScoringError。
    sandbox_files 接受 F09 新结构或旧版 path→content 映射（过渡兼容）。
    """
    if not isinstance(assertions, list):
        raise ScoringError(f"断言必须是数组，得到 {type(assertions).__name__}")

    results: list[dict[str, Any]] = []
    for i, a in enumerate(assertions):
        if not isinstance(a, dict):
            raise ScoringError(f"第 {i + 1} 条断言不是对象: {a!r}")
        t = a.get("type")
        description = _describe(a)

        if t == "file_exists":
            path = a.get("path")
            passed = isinstance(path, str) and path in _manifest_paths(sandbox_files)
            detail = "" if passed else f"沙箱中不存在: {path}"
        elif t == "file_contains":
            path, contains = a.get("path"), a.get("contains")
            if not isinstance(path, str) or not isinstance(contains, str):
                passed, detail = False, "path/contains 字段缺失或类型错误"
            else:
                texts = _text_contents(sandbox_files)
                if path not in texts:
                    if path in _manifest_paths(sandbox_files):
                        passed, detail = False, f"文件 {path} 存在但非文本（二进制），无法断言内容"
                    else:
                        passed, detail = False, f"沙箱中不存在: {path}"
                else:
                    passed = contains in texts[path]
                    detail = "" if passed else f"文件 {path} 不包含目标内容"
        elif t == "text_contains":
            contains = a.get("contains")
            if not isinstance(contains, str):
                passed, detail = False, "contains 字段缺失或类型错误"
            else:
                passed = contains in (final_message or "")
                detail = "" if passed else "最终答复不包含目标内容"
        elif t == "exit_reason_is":
            value = a.get("value")
            if not isinstance(value, str):
                passed, detail = False, "value 字段缺失或类型错误"
            else:
                passed = exit_reason == value
                detail = "" if passed else f"实际退出原因: {exit_reason}"
        else:
            passed, detail = False, f"未知断言类型: {t}"

        results.append({
            "type": str(t),
            "description": description,
            "passed": passed,
            "detail": detail,
        })
    return results
