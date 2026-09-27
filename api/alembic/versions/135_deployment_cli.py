"""Deployment branch tracking, explicit mounts and live CLI approval leases."""
from alembic import op

revision = "135"
down_revision = "134"
branch_labels = None
depends_on = None


def upgrade():
    # Preserve implicit mounts for pre-migration deployments. New rows default off.
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS mount_enabled boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE deployments ALTER COLUMN mount_enabled SET DEFAULT false")
    op.execute("ALTER TABLE deployments DROP CONSTRAINT IF EXISTS ck_deployments_version_pin")
    op.execute("ALTER TABLE deployments ADD CONSTRAINT ck_deployments_version_pin CHECK (version_pin IN ('head','major','specific'))")
    op.execute("ALTER TABLE deployments DROP CONSTRAINT IF EXISTS ck_deployments_pinned_required")
    op.execute("""ALTER TABLE deployments ADD CONSTRAINT ck_deployments_pinned_required CHECK (
        version_pin='head' OR
        (version_pin='major' AND pinned_major IS NOT NULL AND pinned_sub IS NULL) OR
        (version_pin='specific' AND pinned_major IS NOT NULL AND pinned_sub IS NOT NULL))""")
    op.execute("""CREATE TABLE deployment_cli_leases (
        call_id text PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
        run_id text NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        operation text NOT NULL,
        expires_at timestamptz NOT NULL)""")
    op.execute("ALTER TABLE deployment_cli_leases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE deployment_cli_leases FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON deployment_cli_leases
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")


def downgrade():
    # A branch-following deployment cannot safely be converted to global HEAD.
    raise RuntimeError("Deployment CLI migration requires an explicit rollback plan for branch-tracking deployments.")
