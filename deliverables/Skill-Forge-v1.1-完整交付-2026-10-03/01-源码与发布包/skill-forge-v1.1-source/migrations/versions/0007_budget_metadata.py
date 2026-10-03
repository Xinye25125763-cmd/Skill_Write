"""Persist budget compliance separately from task completion."""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("budget_json", sa.Text(), nullable=False, server_default="{}"))


def downgrade():
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("budget_json")
