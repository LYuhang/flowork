"""Fence resident execution ownership and recover abandoned invocation leases."""
from alembic import op

revision = '144'
down_revision = '143'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE deployment_invocations ADD COLUMN runtime_claim uuid')
    op.execute('ALTER TABLE deployment_invocations ADD COLUMN execution_lease_until timestamptz')
    op.execute('ALTER TABLE deployment_invocations ADD COLUMN dispatch_deadline timestamptz')
    op.execute("""UPDATE deployment_invocations SET dispatch_deadline=now()+interval '5 minutes'
        WHERE revision_id IS NOT NULL AND source IN ('sync_api','test') AND status='running'""")
    op.execute("""CREATE INDEX ix_deployment_invocations_execution_lease
        ON deployment_invocations(execution_lease_until)
        WHERE status IN ('queued','running') AND execution_lease_until IS NOT NULL""")


def downgrade():
    raise RuntimeError('Drain resident invocations before an explicit rollback.')
