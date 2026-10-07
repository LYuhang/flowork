"""Notify canvas viewers after committed Workflow metadata/version changes."""
from alembic import op
revision = '173'
down_revision = '172'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE FUNCTION notify_workflow_activity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM pg_notify('flowork_workflow_activity', COALESCE(NEW.wf_id, OLD.wf_id));
      RETURN COALESCE(NEW, OLD);
    END; $$''')
    for table in ('workflows', 'workflow_versions'):
        op.execute(f'''CREATE TRIGGER workflow_activity AFTER INSERT OR UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION notify_workflow_activity()''')


def downgrade():
    for table in ('workflows', 'workflow_versions'):
        op.execute(f'DROP TRIGGER workflow_activity ON {table}')
    op.execute('DROP FUNCTION notify_workflow_activity()')
