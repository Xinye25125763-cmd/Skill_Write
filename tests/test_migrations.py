"""迁移可重放测试：upgrade 幂等、downgrade/upgrade 往返（S1 完成定义）。"""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

ROOT = Path(__file__).resolve().parents[1]


def _alembic_cfg() -> AlembicConfig:
    return AlembicConfig(str(ROOT / "alembic.ini"))


def _tables(db_file: Path) -> set[str]:
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {name for (name,) in rows}


def test_migration_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLFORGE_DATA_DIR", str(tmp_path / "data"))
    db_file = tmp_path / "data" / "app.db"

    command.upgrade(_alembic_cfg(), "head")
    tables = _tables(db_file)
    assert "app_meta" in tables

    # 重放 upgrade：应幂等无错
    command.upgrade(_alembic_cfg(), "head")
    assert "app_meta" in _tables(db_file)

    # 降级后表消失，再升回
    command.downgrade(_alembic_cfg(), "base")
    assert "app_meta" not in _tables(db_file)
    command.upgrade(_alembic_cfg(), "head")
    assert "app_meta" in _tables(db_file)

    # 种子数据在（重放不重复插入：主键约束会阻止，upgrade 幂等即通过）
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute("SELECT value FROM app_meta WHERE key='schema_seed'").fetchall()
    assert rows == [("0001",)]
