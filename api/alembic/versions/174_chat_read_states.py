"""Persist each user's completed-turn read position and notify other views."""
from alembic import op

revision = '174'
down_revision = '173'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE chat_read_states (
            tenant_id uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            chat_id text NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
            run_id text NOT NULL,
            run_created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    op.execute('ALTER TABLE chat_read_states ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE chat_read_states FORCE ROW LEVEL SECURITY')
    op.execute("""
        CREATE POLICY chat_read_states_owner ON chat_read_states
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    """)
    op.execute("""CREATE TRIGGER chat_read_changed AFTER INSERT OR UPDATE ON chat_read_states
        FOR EACH ROW EXECUTE FUNCTION notify_chat_activity()""")


def downgrade():
    op.execute('DROP TABLE chat_read_states')
