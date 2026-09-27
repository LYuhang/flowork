"""Workflow deletion cleanup ledger and live CLI ownership.

Revision ID: 131
Revises: 130
"""
from alembic import op

revision = "131"
down_revision = "130"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE workflow_cli_leases (
            call_id text PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
            workflow_id text NOT NULL REFERENCES workflows(wf_id),
            run_id text NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
            operation text NOT NULL CHECK (operation IN ('run','delete')),
            expires_at timestamptz NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_workflow_cli_leases_workflow ON workflow_cli_leases(workflow_id)")
    op.execute("""CREATE TABLE workflow_deletion_cleanup (
            workflow_id text PRIMARY KEY REFERENCES workflows(wf_id),
            tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
            completed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        );
    """)
    for table in ("workflow_cli_leases", "workflow_deletion_cleanup"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")
    # All producers, including schedules and other API processes, take the
    # same row lock as deletion before activating a dependency. Terminal
    # history remains editable after deletion. No user content in the ledger.
    op.execute("""
        CREATE FUNCTION guard_live_workflow_dependency() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target text; active boolean; live boolean;
        BEGIN
            target := to_jsonb(NEW)->>TG_ARGV[0];
            IF target IS NULL THEN RETURN NEW; END IF;
            IF TG_ARGV[1] = 'enabled' THEN
                active := (to_jsonb(NEW)->>'enabled')::boolean
                    AND (to_jsonb(NEW)->>'deleted_at') IS NULL;
            ELSE
                active := (to_jsonb(NEW)->>'status') = ANY(string_to_array(TG_ARGV[1], ','));
            END IF;
            IF NOT active THEN RETURN NEW; END IF;
            SELECT deleted_at IS NULL INTO live FROM workflows WHERE wf_id=target FOR UPDATE;
            IF live IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION 'Workflow is unavailable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    for table, column, active in (
        ("deployments", "wf_id", "enabled"),
        ("task_schedules", "workflow_id", "enabled"),
        ("tasks", "workflow_id", "queued,running,cancelling,resuming"),
        ("deployment_invocations", "wf_id", "queued,running"),
        ("scheduled_run_executions", "workflow_id", "queued,running"),
        ("workflow_run_state", "wf_id", "pending,running"),
    ):
        op.execute(f"""CREATE TRIGGER workflow_dependency_guard BEFORE INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION guard_live_workflow_dependency('{column}', '{active}')""")


def downgrade():
    for table in ("deployment_invocations", "deployments", "task_schedules", "tasks", "scheduled_run_executions", "workflow_run_state"):
        op.execute(f"DROP TRIGGER workflow_dependency_guard ON {table}")
    op.execute("DROP FUNCTION guard_live_workflow_dependency()")
    op.execute("DROP TABLE workflow_deletion_cleanup")
    op.execute("DROP TABLE workflow_cli_leases")
