"""Notify Preview and Chat background event subscribers on commit."""
from alembic import op

revision = '169'
down_revision = '168'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(VFS_ORDERED_FUNCTION)
    op.execute("""CREATE FUNCTION notify_preview_state() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('flowork_preview_state', NEW.scope_kind || ':' || NEW.scope_id);
            RETURN NEW;
        END;
        $$""")
    op.execute("""CREATE TRIGGER preview_event_appended AFTER INSERT ON vfs_artifact_events
        FOR EACH ROW EXECUTE FUNCTION notify_preview_state()""")
    op.execute("""CREATE FUNCTION notify_background_state() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE chat text;
        BEGIN
            SELECT chat_id INTO chat FROM chat_tool_jobs WHERE job_id = NEW.job_id;
            PERFORM pg_notify('flowork_background_state', chat);
            RETURN NEW;
        END;
        $$""")
    op.execute("""CREATE TRIGGER background_event_appended AFTER INSERT ON chat_tool_job_events
        FOR EACH ROW EXECUTE FUNCTION notify_background_state()""")


def downgrade():
    op.execute(VFS_ORIGINAL_FUNCTION)
    op.execute('DROP TRIGGER background_event_appended ON chat_tool_job_events')
    op.execute('DROP FUNCTION notify_background_state()')
    op.execute('DROP TRIGGER preview_event_appended ON vfs_artifact_events')
    op.execute('DROP FUNCTION notify_preview_state()')


VFS_ORIGINAL_FUNCTION = """
        CREATE OR REPLACE FUNCTION emit_vfs_artifact_event()
        RETURNS trigger AS $$
        DECLARE
            kind text := TG_ARGV[0];
            old_scope text;
            new_scope text;
            emitted_id bigint;
        BEGIN
            IF kind = 'run' THEN
                IF TG_OP <> 'INSERT' THEN old_scope := OLD.run_id; END IF;
                IF TG_OP <> 'DELETE' THEN new_scope := NEW.run_id; END IF;
            ELSE
                IF TG_OP <> 'INSERT' THEN old_scope := OLD.scope_id; END IF;
                IF TG_OP <> 'DELETE' THEN new_scope := NEW.scope_id; END IF;
            END IF;

            IF TG_OP = 'DELETE' OR (
                TG_OP = 'UPDATE' AND (
                    old_scope IS DISTINCT FROM new_scope
                    OR OLD.path IS DISTINCT FROM NEW.path
                )
            ) THEN
                INSERT INTO vfs_artifact_events (
                    tenant_id, scope_kind, scope_id, path,
                    event_type, content_revision
                ) VALUES (
                    OLD.tenant_id, kind, old_scope, OLD.path,
                    'delete', NULL
                ) RETURNING event_id INTO emitted_id;
                PERFORM pg_notify('vfs_artifact_events', emitted_id::text);
            END IF;

            IF TG_OP = 'INSERT' OR (
                TG_OP = 'UPDATE' AND (
                    old_scope IS DISTINCT FROM new_scope
                    OR OLD.path IS DISTINCT FROM NEW.path
                    OR OLD.content_revision IS DISTINCT FROM NEW.content_revision
                )
            ) THEN
                INSERT INTO vfs_artifact_events (
                    tenant_id, scope_kind, scope_id, path,
                    event_type, content_revision
                ) VALUES (
                    NEW.tenant_id, kind, new_scope, NEW.path,
                    'upsert', NEW.content_revision
                ) RETURNING event_id INTO emitted_id;
                PERFORM pg_notify('vfs_artifact_events', emitted_id::text);
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
"""

VFS_ORDERED_FUNCTION = VFS_ORIGINAL_FUNCTION.replace(
    "            IF TG_OP = 'DELETE' OR (",
    "            -- Scope locks precede event ID allocation, including moves across scopes.\n            PERFORM pg_advisory_xact_lock(hashtextextended('preview-events:' || kind || ':' || scope, 0))\n                FROM (SELECT DISTINCT unnest(ARRAY[old_scope, new_scope]) AS scope) AS scopes\n                WHERE scope IS NOT NULL ORDER BY scope;\n\n            IF TG_OP = 'DELETE' OR (",
).replace("                PERFORM pg_notify('vfs_artifact_events', emitted_id::text);\n", "")
