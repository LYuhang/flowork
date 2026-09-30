"""Encrypted Chat drafts with durable, idempotent cross-window operations."""
from alembic import op

revision = '146'
down_revision = '145'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS chat_context_drafts (
        chat_id text PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
        version bigint NOT NULL DEFAULT 0,
        generation bigint NOT NULL DEFAULT 0,
        content_ciphertext text NOT NULL,
        content_nonce text NOT NULL,
        content_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id) ON DELETE RESTRICT
    )''')
    op.execute('''CREATE TABLE IF NOT EXISTS chat_context_draft_operations (
        chat_id text NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
        operation_id text NOT NULL,
        request_digest text NOT NULL,
        applied_version bigint NOT NULL,
        PRIMARY KEY(chat_id, operation_id)
    )''')
    for table in ('chat_context_drafts', 'chat_context_draft_operations'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)''')


def downgrade():
    raise RuntimeError('Export pending Chat drafts before an explicit rollback.')
