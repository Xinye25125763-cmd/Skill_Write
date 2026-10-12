"""Persist complete tool outputs independently from the model context."""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_run_tool_outputs",
        sa.Column("output_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_length", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_agent_run_tool_outputs_run_id", "agent_run_tool_outputs", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_tool_outputs_run_id", table_name="agent_run_tool_outputs")
    op.drop_table("agent_run_tool_outputs")
