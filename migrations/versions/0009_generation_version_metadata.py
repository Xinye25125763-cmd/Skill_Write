"""T10 迁移：skill_versions 生成元数据（工程计划 §5 T10）。

版本可复现性：关联合同（JSON+哈希）、规则哈希、验证报告与来源流程。
全部可空——既有草稿创建的版本不受影响（旧读取路径不变）。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("skill_versions") as batch:
        batch.add_column(sa.Column("generation_flow_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("contract_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("contract_hash", sa.Text(), nullable=True))
        batch.add_column(sa.Column("profile_hash", sa.Text(), nullable=True))
        batch.add_column(sa.Column("report_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("skill_versions") as batch:
        batch.drop_column("report_json")
        batch.drop_column("profile_hash")
        batch.drop_column("contract_hash")
        batch.drop_column("contract_json")
        batch.drop_column("generation_flow_id")
