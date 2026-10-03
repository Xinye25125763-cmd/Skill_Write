"""S5 迁移：agent_runs 执行记录表（细化方案 主§7 run_results 的 v0 子集）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_id", sa.Text(), nullable=True),  # 关联技能版本；NULL = 无技能基线
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),  # running|finished
        sa.Column("exit_reason", sa.Text(), nullable=True),  # completed|budget_exceeded|tool_timeout|model_failed|cancelled
        sa.Column("final_message", sa.Text(), nullable=True),
        sa.Column("steps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("sandbox_files_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("sandbox_dir", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
    )
    op.create_index("ix_agent_runs_project", "agent_runs", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_project", table_name="agent_runs")
    op.drop_table("agent_runs")
