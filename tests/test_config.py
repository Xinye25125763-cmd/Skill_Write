"""配置加载、数据目录与启动自检测试。"""
from pathlib import Path

from app.core.config import AppConfig, ServerConfig, load_config
from app.core.db import create_db_engine, db_path, run_startup_checks
from sqlalchemy import text


def test_config_env_overrides_data_dir(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SKILLFORGE_DATA_DIR", str(tmp_path / "custom-data"))
    cfg = load_config()
    assert cfg.data_dir == tmp_path / "custom-data"


def test_config_env_overrides_port(monkeypatch) -> None:
    monkeypatch.setenv("SKILLFORGE_PORT", "9999")
    cfg = load_config()
    assert cfg.server.port == 9999


def test_config_from_custom_file(tmp_path: Path) -> None:
    cfg_file = tmp_path / "alt-config.yaml"
    cfg_file.write_text("server:\n  port: 9000\ndata_dir: alt-data\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    # F01：相对 data_dir 锚定到配置文件所在目录并规范化为绝对路径
    assert cfg.server.port == 9000
    assert cfg.data_dir == (tmp_path / "alt-data").resolve()


def test_missing_config_file_uses_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "no-such-file.yaml")
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8710


def test_startup_checks_create_dirs_and_db(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SKILLFORGE_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config()
    db = run_startup_checks(cfg)

    assert db.exists()
    assert db == tmp_path / "data" / "app.db"
    assert not (tmp_path / "data" / ".write_probe").exists()  # 探针已清理


def test_engine_pragmas(tmp_path: Path) -> None:
    engine = create_db_engine(tmp_path / "app.db")
    try:
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
            busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
        assert mode == "wal"
        assert fk == 1
        assert busy == 5000
    finally:
        engine.dispose()
