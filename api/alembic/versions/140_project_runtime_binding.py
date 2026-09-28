"""Store the shared Runtime binding on Project.

Revision ID: 140
Revises: 139
"""

from alembic import op

revision = "140"
down_revision = "139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("runtime_type", "runtime_session_id", "runtime_connection_id", "runtime_model_id"):
        op.execute(f"ALTER TABLE chat_projects ADD COLUMN IF NOT EXISTS {column} text")
    op.execute("""
        UPDATE chat_projects SET
            runtime_type = COALESCE(runtime_type, 'codex'),
            runtime_session_id = COALESCE(runtime_session_id, 'rt_codex_' || replace(gen_random_uuid()::text, '-', ''))
    """)


def downgrade() -> None:
    for column in ("runtime_model_id", "runtime_connection_id", "runtime_session_id", "runtime_type"):
        op.execute(f"ALTER TABLE chat_projects DROP COLUMN {column}")
