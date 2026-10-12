"""初始迁移：app_meta 键值表。

用途：承载迁移链种子与后续启动自检标记。S2 起的业务表在各步骤追加迁移。

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.execute("INSERT INTO app_meta(key, value) VALUES ('schema_seed', '0001')")


def downgrade() -> None:
    op.drop_table("app_meta")
