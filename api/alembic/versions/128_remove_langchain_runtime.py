"""Remove LangChain as an installed Agent Runtime.

Revision ID: 128
Revises: 127
"""

from alembic import op


revision = "128"
down_revision = "127"
branch_labels = None
depends_on = None


def _runtime_identifier_constraint(column: str) -> str:
    return f"{column} ~ '^[a-z][a-z0-9_-]{{0,63}}$'"


def upgrade() -> None:
    # Existing development Chats keep their transcript but lose the removed
    # SDK's native state. The next turn binds them to the configured Runtime.
    op.execute(
        "UPDATE chats SET runtime_type=NULL, runtime_session_id=NULL, "
        "runtime_state_ref=NULL, runtime_model_id=NULL, "
        "runtime_connection_id=NULL, runtime_agent_settings=NULL "
        "WHERE runtime_type='langchain'"
    )
    op.execute(
        "UPDATE user_agent_preferences SET default_runtime_type='codex' "
        "WHERE default_runtime_type='langchain'"
    )
    # Saved API connections remain user data and become available to Codex
    # when their provider speaks a supported Responses-compatible protocol.
    op.execute(
        "UPDATE llm_credentials SET runtime_scope='codex' "
        "WHERE runtime_scope='langchain'"
    )
    op.execute("DELETE FROM chat_tool_jobs WHERE runtime_type='langchain'")

    op.execute(
        "ALTER TABLE user_agent_preferences ALTER COLUMN "
        "default_runtime_type SET DEFAULT 'codex'"
    )
    op.execute(
        "ALTER TABLE llm_credentials ALTER COLUMN runtime_scope "
        "SET DEFAULT 'codex'"
    )

    constraints = (
        ("chats", "ck_chats_runtime_type", "runtime_type", True),
        (
            "user_agent_preferences",
            "ck_user_agent_preferences_runtime_type",
            "default_runtime_type",
            False,
        ),
        (
            "llm_credentials",
            "ck_llm_credentials_runtime_scope",
            "runtime_scope",
            False,
        ),
        (
            "chat_tool_jobs",
            "ck_chat_tool_jobs_runtime_type",
            "runtime_type",
            False,
        ),
    )
    for table, name, column, nullable in constraints:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        expression = _runtime_identifier_constraint(column)
        if nullable:
            expression = f"{column} IS NULL OR {expression}"
        op.create_check_constraint(name, table, expression)

    # The removed Runtime was the sole owner of both checkpoint layouts.
    op.execute("DROP TABLE IF EXISTS vc_runtime_checkpoint_writes")
    op.execute("DROP TABLE IF EXISTS vc_runtime_checkpoints")
    op.execute("DROP TABLE IF EXISTS checkpoint_writes")
    op.execute("DROP TABLE IF EXISTS checkpoint_blobs")
    op.execute("DROP TABLE IF EXISTS checkpoints")
    op.execute("DROP TABLE IF EXISTS checkpoint_migrations")


def downgrade() -> None:
    # Runtime state and deleted background jobs are intentionally not
    # reconstructable. Downgrade only restores the former accepted values.
    constraints = (
        ("chats", "ck_chats_runtime_type", "runtime_type", True),
        (
            "user_agent_preferences",
            "ck_user_agent_preferences_runtime_type",
            "default_runtime_type",
            False,
        ),
        (
            "llm_credentials",
            "ck_llm_credentials_runtime_scope",
            "runtime_scope",
            False,
        ),
        (
            "chat_tool_jobs",
            "ck_chat_tool_jobs_runtime_type",
            "runtime_type",
            False,
        ),
    )
    for table, name, column, nullable in constraints:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        expression = f"{column} IN ('langchain','codex')"
        if nullable:
            expression = f"{column} IS NULL OR {expression}"
        op.create_check_constraint(name, table, expression)
    op.execute(
        "ALTER TABLE user_agent_preferences ALTER COLUMN "
        "default_runtime_type SET DEFAULT 'langchain'"
    )
    op.execute(
        "ALTER TABLE llm_credentials ALTER COLUMN runtime_scope "
        "SET DEFAULT 'langchain'"
    )
