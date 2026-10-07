"""Wake task log subscribers after event commits."""
from alembic import op
revision = '168'
down_revision = '167'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE FUNCTION notify_task_state() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('flowork_task_state', NEW.task_id::text);
            RETURN NEW;
        END;
        $$""")
    op.execute("""CREATE TRIGGER task_event_appended AFTER INSERT ON task_events
        FOR EACH ROW EXECUTE FUNCTION notify_task_state()""")

    op.execute("""CREATE FUNCTION notify_task_command() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'tasks' THEN
                PERFORM pg_notify('flowork_task_command', NEW.id::text);
            ELSE
                PERFORM pg_notify('flowork_schedule_command', NEW.id::text);
            END IF;
            RETURN NEW;
        END;
        $$""")
    for table in ('tasks', 'scheduled_run_executions'):
        op.execute(f"""CREATE TRIGGER cancellation_requested AFTER UPDATE OF status ON {table}
            FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status
                AND NEW.status IN ('cancelling', 'cancelled'))
            EXECUTE FUNCTION notify_task_command()""")


def downgrade():
    for table in ('tasks', 'scheduled_run_executions'):
        op.execute(f'DROP TRIGGER cancellation_requested ON {table}')
    op.execute('DROP FUNCTION notify_task_command()')
    op.execute('DROP TRIGGER task_event_appended ON task_events')
    op.execute('DROP FUNCTION notify_task_state()')
