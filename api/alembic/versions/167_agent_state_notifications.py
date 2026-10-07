"""Commit-time Agent event and cancellation wakeups."""
from alembic import op

revision = '167'
down_revision = '166'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION notify_agent_state() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('flowork_agent_state', NEW.run_id);
            IF TG_TABLE_NAME = 'agent_runs' THEN
                IF NEW.status = 'cancel_requested' THEN
                    PERFORM pg_notify('flowork_agent_command', NEW.run_id);
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""CREATE TRIGGER agent_event_appended AFTER INSERT ON agent_run_events
        FOR EACH ROW EXECUTE FUNCTION notify_agent_state()""")
    op.execute("""CREATE TRIGGER agent_run_status_changed AFTER UPDATE OF status ON agent_runs
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION notify_agent_state()""")


def downgrade():
    op.execute('DROP TRIGGER agent_run_status_changed ON agent_runs')
    op.execute('DROP TRIGGER agent_event_appended ON agent_run_events')
    op.execute('DROP FUNCTION notify_agent_state()')
