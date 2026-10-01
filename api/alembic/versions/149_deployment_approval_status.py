"""Keep waiting approvals in deployment capacity and retirement accounting."""

from alembic import op

revision = "149"
down_revision = "148"
branch_labels = None
depends_on = None


def _indexes(active):
    op.execute("DROP INDEX ix_deployment_invocations_revision_pending")
    op.execute("DROP INDEX ix_deployment_invocations_execution_lease")
    op.execute(f"""CREATE INDEX ix_deployment_invocations_revision_pending
        ON deployment_invocations(revision_id) WHERE status IN ({active})""")
    op.execute(f"""CREATE INDEX ix_deployment_invocations_execution_lease
        ON deployment_invocations(execution_lease_until)
        WHERE status IN ({active}) AND execution_lease_until IS NOT NULL""")


def upgrade():
    op.execute("ALTER TABLE deployment_invocations DROP CONSTRAINT ck_deployment_invocations_status")
    op.execute("""ALTER TABLE deployment_invocations ADD CONSTRAINT ck_deployment_invocations_status
        CHECK (status IN ('queued','running','waiting_approval','succeeded','failed','timed_out','cancelled'))""")
    _indexes("'queued','running','waiting_approval'")


def downgrade():
    op.execute("UPDATE deployment_invocations SET status='running' WHERE status='waiting_approval'")
    op.execute("UPDATE deployment_invocations SET status='failed' WHERE status IN ('timed_out','cancelled')")
    op.execute("ALTER TABLE deployment_invocations DROP CONSTRAINT ck_deployment_invocations_status")
    op.execute("""ALTER TABLE deployment_invocations ADD CONSTRAINT ck_deployment_invocations_status
        CHECK (status IN ('queued','running','succeeded','failed'))""")
    _indexes("'queued','running'")
