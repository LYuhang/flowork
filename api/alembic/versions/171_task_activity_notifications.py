"""Task detail invalidations, excluding worker heartbeat-only updates."""
from alembic import op
revision = '171'
down_revision = '170'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE FUNCTION notify_task_activity() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE target uuid;
    BEGIN
        IF TG_TABLE_NAME = 'tasks' THEN
            target := COALESCE(NEW.id, OLD.id);
        ELSIF TG_TABLE_NAME = 'task_schedules' THEN
            target := COALESCE(NEW.task_id, OLD.task_id);
        ELSE
            SELECT task_id INTO target FROM task_schedules
              WHERE id = COALESCE(NEW.schedule_id, OLD.schedule_id);
        END IF;
        IF target IS NOT NULL THEN
            PERFORM pg_notify('flowork_task_activity', target::text);
        END IF;
        RETURN COALESCE(NEW, OLD);
    END; $$''')
    fields = {
        'tasks': ['status', 'progress', 'content_ciphertext', 'results_uri', 'started_at', 'finished_at'],
        'task_schedules': ['enabled', 'schedule_type', 'run_at', 'private_ciphertext', 'mount_enabled',
                           'next_run_at', 'end_at', 'last_run_at', 'last_status'],
        'scheduled_run_executions': ['status', 'private_ciphertext', 'started_at', 'finished_at'],
    }
    for table, columns in fields.items():
        op.execute(f'''CREATE TRIGGER task_activity_created_deleted AFTER INSERT OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION notify_task_activity()''')
        changed = ' OR '.join(f'OLD.{c} IS DISTINCT FROM NEW.{c}' for c in columns)
        op.execute(f'''CREATE TRIGGER task_activity_updated AFTER UPDATE OF {', '.join(columns)} ON {table}
            FOR EACH ROW WHEN ({changed}) EXECUTE FUNCTION notify_task_activity()''')


def downgrade():
    for table in ('tasks', 'task_schedules', 'scheduled_run_executions'):
        op.execute(f'DROP TRIGGER task_activity_updated ON {table}')
        op.execute(f'DROP TRIGGER task_activity_created_deleted ON {table}')
    op.execute('DROP FUNCTION notify_task_activity()')
