"""Notify every open chat observer of committed turn lifecycle changes."""
from alembic import op

revision = '170'
down_revision = '169'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION notify_chat_activity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('flowork_chat_activity', NEW.chat_id);
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""CREATE TRIGGER chat_turn_created AFTER INSERT ON agent_runs
        FOR EACH ROW EXECUTE FUNCTION notify_chat_activity()""")
    op.execute("""CREATE TRIGGER chat_turn_status_changed AFTER UPDATE OF status ON agent_runs
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION notify_chat_activity()""")


def downgrade():
    op.execute('DROP TRIGGER chat_turn_status_changed ON agent_runs')
    op.execute('DROP TRIGGER chat_turn_created ON agent_runs')
    op.execute('DROP FUNCTION notify_chat_activity()')
