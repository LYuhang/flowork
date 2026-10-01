"""Identify batch inputs in execution history without exposing their content."""

from alembic import op

revision = "151"
down_revision = "150"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE workflow_execution_runs ADD COLUMN input_index integer CHECK (input_index >= 0)")


def downgrade():
    op.execute("ALTER TABLE workflow_execution_runs DROP COLUMN input_index")
