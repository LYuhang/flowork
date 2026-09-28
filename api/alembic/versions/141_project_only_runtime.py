"""Finish the Project ownership cutover; remove per-Chat Runtime storage.

Revision ID: 141
Revises: 140

Unowned legacy conversations are intentionally discarded, not wrapped in
compatibility Projects. Accounts, workflows and tasks are preserved.
"""

from alembic import op

revision = "141"
down_revision = "140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migration 001 also builds current ORM metadata on a clean install.
    op.execute("ALTER TABLE chat_projects ADD COLUMN IF NOT EXISTS surface text NOT NULL DEFAULT 'chat'")
    for table in ("vfs_artifact_events", "vfs_scratch", "vfs_artifacts"):
        op.execute(f"DELETE FROM {table} WHERE starts_with(scope_id, '__chatws_v2_')")
    op.execute("DELETE FROM chats WHERE project_id IS NULL")
    op.execute("ALTER TABLE chats ALTER COLUMN project_id SET NOT NULL")
    for column in ("runtime_type", "runtime_session_id", "runtime_connection_id"):
        op.execute(f"ALTER TABLE chats DROP COLUMN IF EXISTS {column}")
    op.execute("ALTER TABLE chat_projects ALTER COLUMN runtime_type SET NOT NULL")
    op.execute("ALTER TABLE chat_projects ALTER COLUMN runtime_session_id SET NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_project_runtime_session ON chat_projects (runtime_session_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_project_chat ON chats (project_id) WHERE surface = 'browser'")


def downgrade() -> None:
    raise RuntimeError("Project ownership cutover is irreversible; restore a pre-migration backup")
