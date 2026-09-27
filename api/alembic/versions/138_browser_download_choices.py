"""Encrypted, turn-scoped native browser download candidate sets."""
from alembic import op

revision = "138"
down_revision = "137"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE browser_download_choices (
        choice_set_id text PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
        user_id uuid NOT NULL REFERENCES users(user_id),
        chat_id text NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
        run_id text NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        runtime_session_id text NOT NULL,
        transport_id text NOT NULL,
        browser_session_id text NOT NULL,
        browser_generation bigint NOT NULL,
        capture_id text NOT NULL,
        tab_id bigint NOT NULL,
        hitl_request_id text REFERENCES hitl_requests(hitl_request_id) ON DELETE SET NULL,
        private_ciphertext text NOT NULL,
        private_nonce text NOT NULL,
        private_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id),
        revoked_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("CREATE INDEX ix_browser_download_choices_capture ON browser_download_choices (tenant_id, run_id, capture_id)")
    op.execute("ALTER TABLE browser_download_choices ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE browser_download_choices FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON browser_download_choices
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")


def downgrade():
    op.execute("DROP TABLE browser_download_choices")
