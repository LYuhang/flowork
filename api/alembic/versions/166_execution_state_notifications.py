"""Commit-time wakeups for execution observers."""
from alembic import op

revision = '166'
down_revision = '165'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION notify_execution_state() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'workflow_execution_approvals' THEN
                PERFORM pg_notify('flowork_execution_state', NEW.execution_id::text);
            ELSE
                PERFORM pg_notify('flowork_execution_state', NEW.id::text);
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    for table, trigger in [('workflow_execution_runs', 'execution_run_state_changed'),
                           ('deployment_invocations', 'deployment_invocation_state_changed')]:
        op.execute(f"""CREATE TRIGGER {trigger} AFTER UPDATE OF status ON {table}
            FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
            EXECUTE FUNCTION notify_execution_state()""")
    op.execute("""CREATE TRIGGER execution_approval_created
        AFTER INSERT ON workflow_execution_approvals
        FOR EACH ROW EXECUTE FUNCTION notify_execution_state()""")


    op.execute("""CREATE TRIGGER execution_approval_decided
        AFTER UPDATE OF status ON workflow_execution_approvals
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION notify_execution_state()""")
    op.execute("""CREATE TRIGGER execution_command_requested
        AFTER UPDATE OF cancel_requested_at, timeout_requested_at ON workflow_execution_runs
        FOR EACH ROW WHEN (
            OLD.cancel_requested_at IS DISTINCT FROM NEW.cancel_requested_at OR
            OLD.timeout_requested_at IS DISTINCT FROM NEW.timeout_requested_at)
        EXECUTE FUNCTION notify_execution_state()""")


def downgrade():
    for table, trigger in [('workflow_execution_runs', 'execution_command_requested'),
                           ('workflow_execution_approvals', 'execution_approval_decided'),
                           ('workflow_execution_approvals', 'execution_approval_created'),
                           ('deployment_invocations', 'deployment_invocation_state_changed'),
                           ('workflow_execution_runs', 'execution_run_state_changed')]:
        op.execute(f'DROP TRIGGER {trigger} ON {table}')
    op.execute('DROP FUNCTION notify_execution_state()')
