"""Fence Task workers and retain liveness independently of node output."""
from alembic import op

revision = "134"
down_revision = "133"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("tasks", "scheduled_run_executions"):
        # Migration 001 creates current ORM metadata on fresh databases.
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS worker_token uuid")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS worker_heartbeat_at timestamptz")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS worker_recovery_pending boolean NOT NULL DEFAULT false")
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_worker_heartbeat ON {table} (status, worker_heartbeat_at)")


def downgrade():
    for table in ("tasks", "scheduled_run_executions"):
        op.drop_index(f"ix_{table}_worker_heartbeat", table_name=table)
        op.drop_column(table, "worker_heartbeat_at")
        op.drop_column(table, "worker_recovery_pending")
        op.drop_column(table, "worker_token")
