"""Introduce Project-owned Agent workspaces and Chat threads.

Revision ID: 139
Revises: 138
"""

from alembic import op


revision = "139"
down_revision = "138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_projects (
            project_id text PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
            creator_user_id uuid NOT NULL REFERENCES users(user_id),
            metadata_ciphertext text NOT NULL,
            metadata_nonce text NOT NULL,
            metadata_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_chat_projects_owner_updated
            ON chat_projects (tenant_id, creator_user_id, updated_at DESC)
            WHERE deleted_at IS NULL
    """)
    op.execute("ALTER TABLE chat_projects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_projects FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE schemaname = current_schema()
                  AND tablename = 'chat_projects'
                  AND policyname = 'chat_projects_owner'
            ) THEN
                CREATE POLICY chat_projects_owner ON chat_projects
                USING (
                    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                    AND creator_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                    AND creator_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                );
            END IF;
        END $$
    """)

    # Project is a new resource boundary, not a compatibility wrapper around
    # the old per-Chat sandbox. The product intentionally starts its main-app
    # Project list clean. Browser-extension conversations remain standalone.
    workspace_mapping = """
        SELECT '__chatws_v2_' || rtrim(
            translate(encode(convert_to(chat_id, 'UTF8'), 'base64'), E'+/\n', '-_'),
            '='
        ) AS scope_id
        FROM chats WHERE surface = 'chat'
    """
    op.execute(f"""
        DELETE FROM vfs_artifact_events
        WHERE scope_id IN ({workspace_mapping})
    """)
    op.execute(f"DELETE FROM vfs_scratch WHERE scope_id IN ({workspace_mapping})")
    op.execute(f"DELETE FROM vfs_artifacts WHERE scope_id IN ({workspace_mapping})")
    op.execute("DELETE FROM chats WHERE surface = 'chat'")

    op.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS project_id text")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'chats'::regclass
                  AND confrelid = 'chat_projects'::regclass
                  AND contype = 'f'
            ) THEN
                ALTER TABLE chats
                ADD CONSTRAINT fk_chats_project
                FOREIGN KEY (project_id) REFERENCES chat_projects(project_id) ON DELETE RESTRICT;
            END IF;
        END $$
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_chats_project_activity
            ON chats (project_id, last_message_at DESC)
            WHERE deleted_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chats_project_activity")
    op.execute("ALTER TABLE chats DROP CONSTRAINT IF EXISTS fk_chats_project")
    op.execute("ALTER TABLE chats DROP COLUMN IF EXISTS project_id")
    op.execute("DROP TABLE chat_projects")
