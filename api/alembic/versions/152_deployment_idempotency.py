"""Durable per-deployment admission receipts; no result or receipt TTL."""

from alembic import op

revision = "152"
down_revision = "151"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE deployment_idempotency_receipts (
        tenant_id uuid NOT NULL,
        deployment_id uuid NOT NULL REFERENCES deployments(id),
        key_digest text NOT NULL,
        invocation_id uuid NOT NULL REFERENCES deployment_invocations(id) DEFERRABLE INITIALLY DEFERRED,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, deployment_id, key_digest)
    )""")
    op.execute("ALTER TABLE deployment_idempotency_receipts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE deployment_idempotency_receipts FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON deployment_idempotency_receipts
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")


def downgrade():
    op.execute("DROP TABLE deployment_idempotency_receipts")
