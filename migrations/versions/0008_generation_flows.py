"""T03 迁移：generation_flows + generation_stage_runs（工程计划 §5 T03）。

generation_flows：一次生成流程 = 一行。快照 JSON 存合同/规则/方案/检查点/预算。
generation_stage_runs：每阶段一行，stage_key 唯一（flow+stage），关联 Job，
同阶段重复排队被唯一键与队列幂等键双重拦截。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_flows",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 原始请求：{request_text, target_platform, resource_ids, profile_revision, budget}
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        # FlowSnapshot 全量 JSON（合同/规则快照/方案/检查点/预算/报告）
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_generation_flows_project", "generation_flows", ["project_id"])

    op.create_table(
        "generation_stage_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "flow_id",
            sa.Text(),
            sa.ForeignKey("generation_flows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text(), nullable=False),  # extract|plan|generate|validate|evaluate|repair|host_check|package
        sa.Column("job_id", sa.Text(), nullable=False),
        # 唯一阶段键：flow_id + stage（同一阶段同一时间只有一个有效任务）
        sa.Column("stage_key", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_generation_stage_runs_flow", "generation_stage_runs", ["flow_id"])


def downgrade() -> None:
    op.drop_index("ix_generation_stage_runs_flow", table_name="generation_stage_runs")
    op.drop_table("generation_stage_runs")
    op.drop_index("ix_generation_flows_project", table_name="generation_flows")
    op.drop_table("generation_flows")
