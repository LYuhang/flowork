"""Own MCP selection at Project scope, without per-Chat compatibility state.

Revision ID: 142
Revises: 141
"""

from alembic import op

revision = "142"
down_revision = "141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_projects ADD COLUMN IF NOT EXISTS mcp_config_revision bigint NOT NULL DEFAULT 0")
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_mcp_bindings (
            project_id text NOT NULL REFERENCES chat_projects(project_id) ON DELETE CASCADE,
            mcp_server_id uuid NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
            tenant_id uuid NOT NULL DEFAULT current_setting('app.tenant_id', true)::uuid
                REFERENCES tenants(tenant_id),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, mcp_server_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_mcp_bindings_server ON project_mcp_bindings (mcp_server_id)")
    op.execute("ALTER TABLE project_mcp_bindings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_mcp_bindings FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY project_mcp_owner ON project_mcp_bindings
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (SELECT 1 FROM chat_projects AS p
                        WHERE p.project_id = project_mcp_bindings.project_id
                          AND p.deleted_at IS NULL)
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (SELECT 1 FROM chat_projects AS p
                        WHERE p.project_id = project_mcp_bindings.project_id
                          AND p.deleted_at IS NULL)
            AND EXISTS (SELECT 1 FROM mcp_servers AS m
                        WHERE m.id = project_mcp_bindings.mcp_server_id
                          AND m.tenant_id = project_mcp_bindings.tenant_id
                          AND m.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                          AND m.deleted_at IS NULL)
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON project_mcp_bindings TO vibecanvas_app")
    # Old selections do not silently grant tools to sibling Chats. The new
    # Project selection starts empty and is explicitly chosen by its owner.
    op.execute("DROP TABLE IF EXISTS chat_mcp_bindings")
    op.execute("ALTER TABLE chats DROP COLUMN IF EXISTS mcp_config_revision")


def downgrade() -> None:
    raise RuntimeError("Project MCP cutover is irreversible; restore a pre-migration backup")
