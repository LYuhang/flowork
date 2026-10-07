"""Wake execution viewers on committed node events and approval changes."""
from alembic import op
revision = '172'
down_revision = '171'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE FUNCTION notify_execution_activity() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE target uuid;
    BEGIN
      IF TG_TABLE_NAME = 'workflow_execution_runs' THEN target := COALESCE(NEW.id, OLD.id);
      ELSE target := COALESCE(NEW.execution_id, OLD.execution_id); END IF;
      PERFORM pg_notify('flowork_execution_activity', target::text);
      RETURN COALESCE(NEW, OLD);
    END; $$''')
    op.execute('''CREATE TRIGGER execution_activity_event AFTER INSERT ON workflow_execution_events
      FOR EACH ROW EXECUTE FUNCTION notify_execution_activity()''')
    op.execute('''CREATE TRIGGER execution_activity_approval AFTER INSERT OR UPDATE OF status ON workflow_execution_approvals
      FOR EACH ROW EXECUTE FUNCTION notify_execution_activity()''')
    op.execute('''CREATE TRIGGER execution_activity_state AFTER UPDATE OF status OR DELETE ON workflow_execution_runs
      FOR EACH ROW EXECUTE FUNCTION notify_execution_activity()''')


def downgrade():
    for table, trigger in [('workflow_execution_events','execution_activity_event'),
                           ('workflow_execution_approvals','execution_activity_approval'),
                           ('workflow_execution_runs','execution_activity_state')]:
        op.execute(f'DROP TRIGGER {trigger} ON {table}')
    op.execute('DROP FUNCTION notify_execution_activity()')
