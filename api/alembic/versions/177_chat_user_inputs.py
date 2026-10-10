"""Persist explicit in-flight user input without replaying execution requests."""
from alembic import op

revision = '177'
down_revision = '176'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
        CREATE TABLE chat_user_inputs (
            input_id text PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            creator_user_id uuid NOT NULL REFERENCES users(user_id),
            chat_id text NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
            run_id text NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
            status text NOT NULL DEFAULT 'submitting'
                CHECK (status IN ('submitting', 'accepted', 'rejected', 'unknown')),
            error_code text,
            content_ciphertext text NOT NULL,
            content_nonce text NOT NULL,
            content_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    ''')
    op.execute('CREATE INDEX chat_user_inputs_chat_run ON chat_user_inputs (tenant_id, chat_id, run_id, created_at)')
    op.execute('ALTER TABLE chat_user_inputs ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE chat_user_inputs FORCE ROW LEVEL SECURITY')
    op.execute('''CREATE POLICY chat_user_inputs_tenant ON chat_user_inputs
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)''')


def downgrade():
    op.execute('DROP TABLE chat_user_inputs')
