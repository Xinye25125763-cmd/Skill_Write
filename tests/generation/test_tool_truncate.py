"""v1.5 M3A：工具输出截断降档 + read_file 按行分页验收。

背景：工具输出随轮次全部重发进模型上下文（历史重发是 token 膨胀
主因，read_file 均值 7.5k 字符/次）。改动：
1. MAX_TOOL_OUTPUT_CHARS 20k → 4k；
2. read_file 新增 offset（1 起始行号）/ limit（行数）分页参数，
   截断提示引导模型分段读取，而非重读同样的截断前缀；
3. 执行器库默认轮次 40 → 15（与 M2 的处理器默认对齐）。
"""
from __future__ import annotations

import json
from pathlib import Path

import app.executors.api_executor as ax_mod
import app.tools.registry as reg


def test_tool_output_cap_lowered_to_4k(tmp_path: Path) -> None:
    """超过 4k 字符的工具输出被截断，且提示里带分页指引。"""
    big = tmp_path / "big.md"
    big.write_text("x" * 10_000, encoding="utf-8")
    out = reg.read_file(tmp_path, {"path": "big.md"})
    assert len(out) <= reg.MAX_TOOL_OUTPUT_CHARS + 200, len(out)
    assert "已截断，原始 10000 字符" in out
    assert "offset/limit" in out
    assert reg.MAX_TOOL_OUTPUT_CHARS == 4_000


def test_script_full_output_preserved_before_context_truncation(tmp_path: Path) -> None:
    """stdout/stderr 原始结果可完整保存；模型回填仍受 4k 限制。"""
    (tmp_path / "emit.py").write_text(
        "print('A' * 6000)\nimport sys; print('B' * 6000, file=sys.stderr)\n",
        encoding="utf-8",
    )
    result = reg.execute_tool_output(tmp_path, "run_script", {"path": "emit.py"})
    assert len(result.visible) <= reg.MAX_TOOL_OUTPUT_CHARS + 100
    payload = json.loads(result.full)
    assert len(payload["stdout"]) == 6001
    assert len(payload["stderr"]) == 6001
    assert payload["stdout"][3000] == "A"
    assert payload["stderr"][3000] == "B"


def test_read_file_paging_by_lines(tmp_path: Path) -> None:
    """offset/limit 按行分页：行号头 + 精确切片 + 越界/非法入参兑底。"""
    f = tmp_path / "lines.md"
    f.write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")

    # 第一页：offset=1, limit=5
    out = reg.read_file(tmp_path, {"path": "lines.md", "offset": 1, "limit": 5})
    assert out.startswith("[行 1-5 / 共 20 行]\n")
    assert "line1" in out and "line5" in out and "line6" not in out

    # 第二页：offset=6（limit 缺省读到文件尾）
    out = reg.read_file(tmp_path, {"path": "lines.md", "offset": 6})
    assert out.startswith("[行 6-20 / 共 20 行]\n")
    assert "line6" in out and "line20" in out

    # offset 越界：错误 JSON，不假装有内容
    out = reg.read_file(tmp_path, {"path": "lines.md", "offset": 99})
    assert json.loads(out)["error"] == "offset_out_of_range"

    # 非法入参：兑底错误 JSON
    out = reg.read_file(tmp_path, {"path": "lines.md", "offset": "abc"})
    assert json.loads(out)["error"] == "bad_offset_limit"

    # 小文件无分页参数：行为不变（全文直出）
    out = reg.read_file(tmp_path, {"path": "lines.md"})
    assert "line1" in out and "line20" in out and "[行" not in out


def test_read_file_paging_escapes_history_bloat(tmp_path: Path) -> None:
    """分页读取大文件：每页输出不超 4k，多页可拼出全文。"""
    f = tmp_path / "huge.md"
    lines = [f"{'y' * 60} 第{i}行" for i in range(1, 301)]  # ~18k 字符
    f.write_text("\n".join(lines), encoding="utf-8")

    whole = reg.read_file(tmp_path, {"path": "huge.md"})
    assert "已截断" in whole  # 不分页必然截断

    pages = [reg.read_file(tmp_path, {"path": "huge.md", "offset": i, "limit": 50})
             for i in range(1, 301, 50)]
    assert all(len(p) <= reg.MAX_TOOL_OUTPUT_CHARS + 200 for p in pages)
    joined = "\n".join(p.split("\n", 1)[1] for p in pages)
    assert "第1行" in joined and "第300行" in joined


def test_executor_default_rounds_aligned_to_15() -> None:
    """执行器库默认轮次与 M2 处理器默认一致（生产路径显式传参）。"""
    assert ax_mod.MAX_ROUNDS_DEFAULT == 15
