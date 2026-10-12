"""v1.4 R2 迁移：eval_case_results 增加 attempt 序号列。

方差分析（repeats>1）需要区分同一用例的多次尝试；存量行即 attempt=1
（repeats=1 现行为，读取路径不变）。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("eval_case_results") as batch:
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False,
                                   server_default="1"))


def downgrade() -> None:
    with op.batch_alter_table("eval_case_results") as batch:
        batch.drop_column("attempt")
