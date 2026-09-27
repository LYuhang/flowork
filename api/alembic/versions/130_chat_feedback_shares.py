"""Message feedback and revocable encrypted conversation snapshots.

Revision ID: 130
Revises: 129
"""
from alembic import op

revision = "130"
down_revision = "129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE chat_message_feedback (
            tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
            user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            chat_id text NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
            message_id text NOT NULL,
            turn_id text,
            rating text CHECK (rating IN ('up','down')),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, chat_id, message_id),
            FOREIGN KEY (chat_id, message_id)
                REFERENCES chat_messages(chat_id, message_id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE TABLE chat_feedback_events (
            id bigserial PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
            user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            chat_id text NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
            message_id text NOT NULL,
            turn_id text,
            rating text CHECK (rating IN ('up','down')),
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (chat_id, message_id)
                REFERENCES chat_messages(chat_id, message_id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX ix_chat_feedback_turn ON chat_message_feedback(tenant_id, turn_id)")
    op.execute("""
        CREATE TABLE chat_public_shares (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
            user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            chat_id text NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
            message_id text,
            token_hash text NOT NULL UNIQUE,
            snapshot_ciphertext text NOT NULL,
            snapshot_nonce text NOT NULL,
            snapshot_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            expires_at timestamptz
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_chat_public_share_active ON chat_public_shares
            (user_id, chat_id, COALESCE(message_id, '')) WHERE revoked_at IS NULL
    """)
    for table in ("chat_message_feedback", "chat_feedback_events", "chat_public_shares"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_owner ON {table}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """)
    # Public discovery admits exactly one unguessable capability, never a
    # tenant-wide read. The endpoint binds tenant context only after this match.
    op.execute("""
        CREATE POLICY chat_public_shares_capability ON chat_public_shares FOR SELECT
        USING (token_hash = NULLIF(current_setting('app.share_token_hash', true), '')
            AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now()))
    """)


def downgrade() -> None:
    for table in ("chat_public_shares", "chat_feedback_events", "chat_message_feedback"):
        op.execute(f"DROP TABLE {table}")
