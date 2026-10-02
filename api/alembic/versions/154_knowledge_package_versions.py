"""Encrypted Knowledge drafts and immutable published package snapshots."""
from alembic import op

revision = '154'
down_revision = '153'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS knowledge_package_snapshots (
        kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
        version integer NOT NULL CHECK (version >= 0),
        base_version integer NOT NULL,
        content_hash text NOT NULL,
        file_count integer NOT NULL,
        size_bytes bigint NOT NULL,
        content_ciphertext text NOT NULL,
        content_nonce text NOT NULL,
        content_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(kb_id, version)
    )''')
    op.execute('ALTER TABLE knowledge_package_snapshots ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE knowledge_package_snapshots FORCE ROW LEVEL SECURITY')
    op.execute('''CREATE POLICY tenant_isolation ON knowledge_package_snapshots
        USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)''')


def downgrade():
    raise RuntimeError('Export Knowledge drafts and history before an explicit rollback.')
