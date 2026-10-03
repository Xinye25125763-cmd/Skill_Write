"""S6 迁移：测试集 + 对照评测运行 + 逐例结果（细化方案 主§7 的最小子集）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suites",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # cases_json: [{id, name, prompt, assertions: [{type, ...}]}]
        sa.Column("cases_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_suites_project", "suites", ["project_id"])

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "suite_id",
            sa.Text(),
            sa.ForeignKey("suites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_id", sa.Text(), nullable=False),  # 候选技能版本
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),  # running|finished
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
    )
    op.create_index("ix_eval_runs_project", "eval_runs", ["project_id"])

    op.create_table(
        "eval_case_results",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "eval_run_id",
            sa.Text(),
            sa.ForeignKey("eval_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("group_name", sa.Text(), nullable=False),  # candidate | baseline
        sa.Column("agent_run_id", sa.Text(), nullable=True),
        # exec_status: executed | exec_failed | skipped（取消/前置失败）
        sa.Column("exec_status", sa.Text(), nullable=False),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        # score_status: scored | score_failed | not_scored（执行失败则不评分）
        sa.Column("score_status", sa.Text(), nullable=False, server_default="not_scored"),
        # assertions_json: [{type, description, passed, detail}]
        sa.Column("assertions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("passed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("score_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_eval_case_results_run", "eval_case_results", ["eval_run_id"])


def downgrade() -> None:
    op.drop_index("ix_eval_case_results_run", table_name="eval_case_results")
    op.drop_table("eval_case_results")
    op.drop_index("ix_eval_runs_project", table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_index("ix_suites_project", table_name="suites")
    op.drop_table("suites")
