"""Record OS process identity for loss detection, never execution recovery."""

from alembic import op

revision = "153"
down_revision = "152"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE workflow_execution_runs ADD COLUMN runtime_process jsonb")
    op.execute("""CREATE INDEX ix_workflow_live_process ON workflow_execution_runs
        ((runtime_process->>'host_id')) WHERE status IN ('running','waiting_approval')
        AND runtime_process IS NOT NULL""")


def downgrade():
    op.execute("DROP INDEX ix_workflow_live_process")
    op.execute("ALTER TABLE workflow_execution_runs DROP COLUMN runtime_process")
