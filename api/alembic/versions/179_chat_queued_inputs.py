"""One explicitly queued next input per Chat, without automatic replay."""
from alembic import op

revision = '179'
down_revision = '178'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE chat_queued_inputs (
        input_id text PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        creator_user_id uuid NOT NULL REFERENCES users(user_id),
        chat_id text NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
        after_run_id text NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        status text NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'claimed', 'started', 'cancelled', 'blocked', 'failed', 'unknown')),
        error_code text,
        started_run_id text REFERENCES agent_runs(run_id),
        content_ciphertext text NOT NULL,
        content_nonce text NOT NULL,
        content_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )''')
    op.execute("""CREATE UNIQUE INDEX chat_queued_inputs_pending ON chat_queued_inputs(chat_id)
        WHERE status IN ('queued', 'claimed', 'blocked', 'unknown')""")
    op.execute('CREATE INDEX chat_queued_inputs_history ON chat_queued_inputs(tenant_id, chat_id, created_at)')
    op.execute("""CREATE TRIGGER chat_queue_created AFTER INSERT ON chat_queued_inputs
        FOR EACH ROW EXECUTE FUNCTION notify_chat_activity()""")
    op.execute("""CREATE TRIGGER chat_queue_status_changed AFTER UPDATE OF status ON chat_queued_inputs
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION notify_chat_activity()""")
    op.execute('ALTER TABLE chat_queued_inputs ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE chat_queued_inputs FORCE ROW LEVEL SECURITY')
    op.execute('''CREATE POLICY chat_queued_inputs_tenant ON chat_queued_inputs
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)''')


def downgrade():
    op.execute('DROP TABLE chat_queued_inputs')
