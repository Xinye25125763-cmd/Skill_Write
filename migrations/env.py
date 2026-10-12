"""Alembic 环境：数据库 URL 由应用配置动态决定（config.yaml / SKILLFORGE_DATA_DIR）。"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import load_config
from app.core.db import db_path

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 数据库位置与运行时保持同源：都走 load_config()
_cfg = load_config()
db_file = db_path(_cfg)
db_file.parent.mkdir(parents=True, exist_ok=True)
_url = "sqlite:///" + str(db_file).replace("\\", "/")
config.set_main_option("sqlalchemy.url", _url)

target_metadata = None  # 迁移手写（主§15：迁移文件随交付），暂不接 autogenerate


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直连数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
