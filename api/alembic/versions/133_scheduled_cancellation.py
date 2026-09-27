"""Keep cancellation pending until the scheduled worker has stopped."""
from alembic import op

revision = "133"
down_revision = "132"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_scheduled_run_executions_status", "scheduled_run_executions", type_="check")
    op.create_check_constraint("ck_scheduled_run_executions_status", "scheduled_run_executions",
        "status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled', 'skipped')")


def downgrade():
    # Refuse to discard an in-flight cancellation on downgrade.
    op.drop_constraint("ck_scheduled_run_executions_status", "scheduled_run_executions", type_="check")
    op.create_check_constraint("ck_scheduled_run_executions_status", "scheduled_run_executions",
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped')")
