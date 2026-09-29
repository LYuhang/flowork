"""Resident deployment revisions and atomic traffic switching."""
from alembic import op

revision = "143"
down_revision = "142"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS cpu_millis integer NOT NULL DEFAULT 500")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS memory_mb integer NOT NULL DEFAULT 256")
    op.execute("ALTER TABLE deployments ADD CONSTRAINT deployment_cpu_budget CHECK (cpu_millis BETWEEN 100 AND 256000)")
    op.execute("ALTER TABLE deployments ADD CONSTRAINT deployment_memory_budget CHECK (memory_mb BETWEEN 128 AND 1048576)")
    op.execute("""CREATE TABLE IF NOT EXISTS deployment_runtime_revisions (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
        deployment_id uuid NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
        spec jsonb NOT NULL,
        state text NOT NULL DEFAULT 'preparing'
            CHECK (state IN ('preparing','active','draining','retired','failed')),
        created_at timestamptz NOT NULL DEFAULT now(),
        activated_at timestamptz,
        runtime_metrics jsonb,
        observed_at timestamptz,
        retired_at timestamptz
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_deployment_runtime_live ON deployment_runtime_revisions(deployment_id, state)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_deployment_runtime_one_active ON deployment_runtime_revisions(deployment_id) WHERE state='active'")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_deployment_runtime_one_candidate ON deployment_runtime_revisions(deployment_id) WHERE state='preparing'")
    op.execute("ALTER TABLE deployment_runtime_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE deployment_runtime_revisions FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON deployment_runtime_revisions
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON deployment_runtime_revisions TO vibecanvas_app")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS active_revision_id uuid REFERENCES deployment_runtime_revisions(id)")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS rollout_status text NOT NULL DEFAULT 'pending'")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS rollout_error text")
    op.execute("ALTER TABLE deployment_invocations ADD COLUMN IF NOT EXISTS revision_id uuid REFERENCES deployment_runtime_revisions(id)")
    op.execute("""CREATE INDEX IF NOT EXISTS ix_deployment_invocations_revision_pending
        ON deployment_invocations(revision_id) WHERE status IN ('queued','running')""")


def downgrade():
    raise RuntimeError("Resident deployments must be drained before an explicit rollback.")
