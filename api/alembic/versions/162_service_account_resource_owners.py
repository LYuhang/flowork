"""Record the resource organization separately from the execution account."""
from alembic import op

revision = "162"
down_revision = "161"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE service_account_resources ADD COLUMN resource_tenant_id uuid REFERENCES tenants(tenant_id) ON DELETE CASCADE")
    # Before this change, dependency binding only admitted resources from the
    # account organization. Backfill once; new writes must provide ownership.
    op.execute("UPDATE service_account_resources SET resource_tenant_id=tenant_id")
    op.execute("ALTER TABLE service_account_resources ALTER COLUMN resource_tenant_id SET NOT NULL")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM service_account_resources WHERE resource_tenant_id<>tenant_id) THEN
            RAISE EXCEPTION 'Cross-organization dependencies must be removed before downgrade';
        END IF;
    END $$""")
    op.drop_column("service_account_resources", "resource_tenant_id")
