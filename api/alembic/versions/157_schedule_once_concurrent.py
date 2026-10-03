"""One-time schedules and independent overlapping occurrences."""
from alembic import op

revision = "157"
down_revision = "156"
branch_labels = None
depends_on = None


def upgrade():
    # Early migrations create some tables from current metadata on fresh installs.
    op.execute("ALTER TABLE task_schedules ADD COLUMN IF NOT EXISTS run_at timestamptz")
    op.drop_constraint("ck_task_schedules_type", "task_schedules", type_="check")
    op.create_check_constraint("ck_task_schedules_type", "task_schedules", "schedule_type IN ('interval', 'cron', 'once')")
    op.execute("ALTER TABLE task_schedules DROP CONSTRAINT IF EXISTS ck_task_schedules_run_at")
    op.create_check_constraint("ck_task_schedules_run_at", "task_schedules", "(schedule_type = 'once') = (run_at IS NOT NULL)")
    op.drop_constraint("ck_task_schedules_concurrency_policy", "task_schedules", type_="check")
    op.execute("UPDATE task_schedules SET concurrency_policy = 'allow'")
    op.alter_column("task_schedules", "concurrency_policy", server_default="allow")
    op.create_check_constraint("ck_task_schedules_concurrency_policy", "task_schedules", "concurrency_policy IN ('allow')")


def downgrade():
    # Retain one-time task history: refuse an incompatible rollback rather than
    # silently deleting tasks or converting them into recurring schedules.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM task_schedules WHERE schedule_type = 'once') THEN
            RAISE EXCEPTION 'Remove one-time schedules explicitly before downgrading';
        END IF;
    END $$""")
    op.drop_constraint("ck_task_schedules_concurrency_policy", "task_schedules", type_="check")
    op.execute("UPDATE task_schedules SET concurrency_policy = 'skip_if_running'")
    op.alter_column("task_schedules", "concurrency_policy", server_default="skip_if_running")
    op.create_check_constraint("ck_task_schedules_concurrency_policy", "task_schedules", "concurrency_policy IN ('skip_if_running')")
    op.drop_constraint("ck_task_schedules_run_at", "task_schedules", type_="check")
    op.drop_constraint("ck_task_schedules_type", "task_schedules", type_="check")
    op.create_check_constraint("ck_task_schedules_type", "task_schedules", "schedule_type IN ('interval', 'cron')")
    op.drop_column("task_schedules", "run_at")
