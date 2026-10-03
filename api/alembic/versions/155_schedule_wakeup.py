"""Wake deadline-driven dispatchers after committed schedule changes."""
from alembic import op

revision = '155'
down_revision = '154'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE FUNCTION notify_flowork_schedule_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            PERFORM pg_notify('flowork_schedule_changed', '');
            RETURN NULL;
        END $$''')
    op.execute('''CREATE TRIGGER task_schedule_changed
        AFTER INSERT OR DELETE OR UPDATE OF enabled, next_run_at, end_at,
            schedule_type, cron_expr, interval_seconds, timezone
        ON task_schedules FOR EACH STATEMENT
        EXECUTE FUNCTION notify_flowork_schedule_changed()''')


def downgrade():
    op.execute('DROP TRIGGER IF EXISTS task_schedule_changed ON task_schedules')
    op.execute('DROP FUNCTION IF EXISTS notify_flowork_schedule_changed()')
