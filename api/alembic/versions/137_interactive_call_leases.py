"""Live-tool-only user choice leases."""
from alembic import op

revision = "137"
down_revision = "136"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE interactive_call_leases (
        call_id text PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
        run_id text NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        expires_at timestamptz NOT NULL)""")
    op.execute("ALTER TABLE interactive_call_leases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE interactive_call_leases FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON interactive_call_leases
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")


def downgrade():
    op.execute("DROP TABLE interactive_call_leases")
