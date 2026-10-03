"""F01 回归：相对 data_dir 锚定 + 相对沙箱下脚本可执行。

缺陷背景：默认 data_dir 为相对路径时，脚本路径与 cwd 双拼导致
run_script 退出码 2；不同 cwd 的父子进程可能使用不同数据目录。
"""
from pathlib import Path

import pytest

from app.core.config import default_config_path, load_config
from app.tools.registry import run_script


def test_relative_yaml_data_dir_anchored_to_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.yaml 里的相对 data_dir 锚定到该文件所在目录，不受 cwd 影响。"""
    cfg_file = tmp_path / "conf" / "config.yaml"
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text("data_dir: mydata\n", encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # 换一个 cwd 验证锚定不随 cwd 变
    cfg = load_config(cfg_file)

    assert cfg.data_dir == (cfg_file.parent / "mydata").resolve()
    assert cfg.data_dir.is_absolute()


def test_env_relative_data_dir_anchored_to_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量给的相对 data_dir 同样锚定到项目根（不校验磁盘，仅路径语义）。"""
    monkeypatch.setenv("SKILLFORGE_DATA_DIR", "somewhere")
    monkeypatch.delenv("SKILLFORGE_CONFIG", raising=False)
    cfg = load_config()
    assert cfg.data_dir == (default_config_path().parent / "somewhere").resolve()
    assert cfg.data_dir.is_absolute()


def test_load_config_cross_cwd_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个不同 cwd 下加载同一配置 → 数据目录一致（启动器与子进程不再分叉）。"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("data_dir: data\n", encoding="utf-8")

    dirs = []
    for sub in ("cwd-a", "cwd-b"):
        d = tmp_path / sub
        d.mkdir()
        monkeypatch.chdir(d)
        dirs.append(load_config(cfg_file).data_dir)
    assert dirs[0] == dirs[1] == (tmp_path / "data").resolve()


def test_run_script_with_relative_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F01 核心复现场景：沙箱本身是相对路径时脚本也能正确执行。

    旧行为：脚本路径相对 + cwd=沙箱 → 子进程把路径再拼一次前缀，退出码 2。
    """
    import json

    monkeypatch.chdir(tmp_path)
    sandbox = Path("sb")  # 故意保持相对（模拟修复前的配置缺陷场景）
    (sandbox / "hello.py").parent.mkdir(parents=True, exist_ok=True)
    (sandbox / "hello.py").write_text("print('ok-run')\n", encoding="utf-8")

    result = json.loads(run_script(sandbox, {"path": "hello.py"}))
    assert result["exit_code"] == 0, result
    assert "ok-run" in result["stdout"]


def test_run_script_py_script_reads_sibling_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """脚本以 cwd=沙箱运行，可读取同目录数据文件（相对 cwd 语义不受修复影响）。"""
    import json

    monkeypatch.chdir(tmp_path)
    sandbox = Path("sb2")
    (sandbox / "data.txt").parent.mkdir(parents=True, exist_ok=True)
    (sandbox / "data.txt").write_text("42\n", encoding="utf-8")
    (sandbox / "read.py").write_text(
        "from pathlib import Path\nprint(Path('data.txt').read_text().strip())\n",
        encoding="utf-8",
    )

    result = json.loads(run_script(sandbox, {"path": "read.py"}))
    assert result["exit_code"] == 0, result
    assert "42" in result["stdout"]
