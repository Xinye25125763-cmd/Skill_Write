"""S4 迁移：jobs 持久化队列 + workflow_events 事件表（细化方案 §4.1 / §3.3）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("heartbeat_at", sa.Float(), nullable=True),
        sa.Column("cancel_requested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_jobs_type_idem", "jobs", ["type", "idempotency_key"], unique=True
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_table(
        "workflow_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("progress_json", sa.Text(), nullable=True),
        sa.Column("ts", sa.Float(), nullable=False),
    )
    op.create_index("ix_events_job", "workflow_events", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_events_job", table_name="workflow_events")
    op.drop_table("workflow_events")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_type_idem", table_name="jobs")
    op.drop_table("jobs")
