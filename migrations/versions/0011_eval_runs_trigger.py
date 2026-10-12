"""v1.4 R1 迁移：eval_runs 增加 kind 与 meta_json。

- kind：区分对照评测（contrast，默认）与触发验证（trigger）——
  导出折算按 kind 定位触发验证运行；
- meta_json：触发验证的轮次/停止原因/评分摘要等元数据（对照评测不用，
  保持 NULL）。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("eval_runs") as batch:
        batch.add_column(sa.Column("kind", sa.Text(), nullable=False,
                                   server_default="contrast"))
        batch.add_column(sa.Column("meta_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("eval_runs") as batch:
        batch.drop_column("meta_json")
        batch.drop_column("kind")
