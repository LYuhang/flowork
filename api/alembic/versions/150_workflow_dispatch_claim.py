"""Fence history-backed task and CLI dispatch before a sandbox process starts."""

from alembic import op

revision = "150"
down_revision = "149"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE workflow_execution_runs ADD COLUMN dispatch_claim uuid")


def downgrade():
    op.execute("ALTER TABLE workflow_execution_runs DROP COLUMN dispatch_claim")
