"""Snapshot invocation execution budgets and durable timeout requests."""
from alembic import op
revision = '156'
down_revision = '155'
branch_labels = None
depends_on = None

def upgrade():
    op.execute('ALTER TABLE deployments ADD COLUMN timeout_seconds integer NOT NULL DEFAULT 30 CHECK (timeout_seconds BETWEEN 1 AND 3600)')
    op.execute('ALTER TABLE deployment_invocations ADD COLUMN timeout_seconds integer CHECK (timeout_seconds BETWEEN 1 AND 3600)')
    op.execute('ALTER TABLE workflow_execution_runs ADD COLUMN timeout_requested_at timestamptz')

def downgrade():
    op.execute('ALTER TABLE workflow_execution_runs DROP COLUMN timeout_requested_at')
    op.execute('ALTER TABLE deployment_invocations DROP COLUMN timeout_seconds')
    op.execute('ALTER TABLE deployments DROP COLUMN timeout_seconds')
