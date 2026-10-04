"""Separate a durable instance's organization from its Workflow's organization."""
from alembic import op

revision = '163'
down_revision = '162'
branch_labels = None
depends_on = None

TABLES = ('deployments', 'tasks', 'task_schedules', 'deployment_invocations',
          'scheduled_run_executions', 'workflow_run_state')


def upgrade():
    for table in TABLES:
        op.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS workflow_tenant_id uuid REFERENCES tenants(tenant_id)')
        # The old guard admitted only same-organization references. Backfill
        # once under table locks, without firing execution-state triggers.
        op.execute(f'ALTER TABLE {table} DISABLE TRIGGER workflow_dependency_guard')
        op.execute(f'ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY')
        op.execute(f'UPDATE {table} SET workflow_tenant_id=tenant_id WHERE workflow_tenant_id IS NULL')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} ENABLE TRIGGER workflow_dependency_guard')
        op.execute(f"ALTER TABLE {table} ALTER COLUMN workflow_tenant_id DROP DEFAULT")
        op.execute(f'ALTER TABLE {table} ALTER COLUMN workflow_tenant_id SET NOT NULL')
    op.execute('ALTER TABLE deployment_runtime_revisions NO FORCE ROW LEVEL SECURITY')
    op.execute("UPDATE deployment_runtime_revisions SET spec=jsonb_set(spec, '{workflow_tenant_id}', to_jsonb(tenant_id::text))")
    op.execute('ALTER TABLE deployment_runtime_revisions FORCE ROW LEVEL SECURITY')
    op.execute("""
        CREATE OR REPLACE FUNCTION guard_live_workflow_dependency() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target text; active boolean; live boolean; previous_tenant text;
        BEGIN
            -- Same-organization references use the instance owner by default;
            -- shared references must carry the host-validated source owner.
            IF NEW.workflow_tenant_id IS NULL THEN
                NEW.workflow_tenant_id := NEW.tenant_id;
            END IF;
            target := to_jsonb(NEW)->>TG_ARGV[0];
            IF target IS NULL THEN RETURN NEW; END IF;
            IF TG_ARGV[1] = 'enabled' THEN
                active := (to_jsonb(NEW)->>'enabled')::boolean
                    AND (to_jsonb(NEW)->>'deleted_at') IS NULL;
            ELSE
                active := (to_jsonb(NEW)->>'status') = ANY(string_to_array(TG_ARGV[1], ','));
            END IF;
            IF NOT active THEN RETURN NEW; END IF;
            previous_tenant := current_setting('app.tenant_id', true);
            PERFORM set_config('app.tenant_id', NEW.workflow_tenant_id::text, true);
            SELECT deleted_at IS NULL INTO live FROM workflows WHERE wf_id=target FOR UPDATE;
            PERFORM set_config('app.tenant_id', COALESCE(previous_tenant, ''), true);
            IF live IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION 'Workflow is unavailable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)


def downgrade():
    raise RuntimeError('Workflow dependency ownership cannot be dropped while cross-organization instances exist.')
