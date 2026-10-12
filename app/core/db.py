"""数据库引擎与会话：SQLite + WAL + 短事务纪律。

S1 阶段仅提供引擎工厂与启动自检；会话工厂随 S2 的首个数据模型引入。
"""
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app.core.config import AppConfig


def db_path(cfg: AppConfig) -> Path:
    """数据库文件路径：{data_dir}/app.db。"""
    return cfg.data_dir / "app.db"


def create_db_engine(path: Path) -> Engine:
    """创建带 PRAGMA 纪律的引擎（WAL / 外键 / 忙等待，主§9）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def run_startup_checks(cfg: AppConfig) -> Path:
    """启动一致性检查（S1 最小版）：数据目录可建、可写，数据库可达。

    S3+ 将扩展为孤立文件 / 缺失产物 / 未完成提交的识别与隔离（主§9）。
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    probe = cfg.data_dir / ".write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()

    engine = create_db_engine(db_path(cfg))
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    return db_path(cfg)
