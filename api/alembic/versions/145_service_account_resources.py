"""Persist explicitly delegated workflow Skill and MCP dependencies."""
from alembic import op

revision = "145"
down_revision = "144"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE service_account_resources (
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        service_account_id uuid NOT NULL REFERENCES service_accounts(service_account_id) ON DELETE CASCADE,
        resource_type text NOT NULL CHECK(resource_type IN ('skill_installation','mcp_installation')),
        resource_id uuid NOT NULL,
        revoked_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(service_account_id,resource_type,resource_id)
    )""")
    op.execute("ALTER TABLE service_account_resources ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE service_account_resources FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON service_account_resources
        USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)""")


def downgrade():
    raise RuntimeError("Revoke delegated resource grants before an explicit rollback.")
