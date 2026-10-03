"""S2 迁移：projects 与 brief_revisions 表。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("target_platform", sa.Text(), nullable=False, server_default="catpaw"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "brief_revisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_revision_id",
            sa.Text(),
            sa.ForeignKey("brief_revisions.id"),
            nullable=True,
        ),
        sa.Column("brief_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="confirmed"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("brief_revisions")
    op.drop_table("projects")
