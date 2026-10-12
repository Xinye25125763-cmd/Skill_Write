"""S3 迁移：skill_versions 表（不可变版本，主§7 / 细化方案 §6）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parent_version_id", sa.Text(), sa.ForeignKey("skill_versions.id"), nullable=True),
        sa.Column("brief_revision_id", sa.Text(), sa.ForeignKey("brief_revisions.id"), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),  # 可编辑名称，与内部 ID 分离
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("files_json", sa.Text(), nullable=False),
        sa.Column("warnings_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="validated"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    # 同项目内相同内容哈希 → 冲突（细化方案 S3 完成定义：409）
    op.create_index(
        "ix_skill_versions_project_hash", "skill_versions", ["project_id", "content_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_skill_versions_project_hash", table_name="skill_versions")
    op.drop_table("skill_versions")
