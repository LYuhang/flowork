"""SQLAlchemy 2.0 declarative models for the application schema.

One Mapped class per table. JSONB for workflow content / tags / meta.
The original business tables carry a UUID tenant_id FK and are protected by
Postgres row-level security; authentication tables are intentionally RLS-free.
(The `refs` table was dropped in VFS 2b-3 — see migration 013.)
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, FetchedValue, ForeignKey,
    Identity, Index, Integer, LargeBinary, Text, TIMESTAMP,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _ts() -> Mapped[datetime]:
    return mapped_column(TIMESTAMP(timezone=True), server_default=func.now(),
                         nullable=False)


class Workflow(Base):
    __tablename__ = "workflows"
    __allow_unmapped__ = True
    wf_id:         Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"))
    creator_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    # Structural source metadata for the initial OpenFGA manager edge.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    metadata_ciphertext: Mapped[str] = mapped_column(
        Text, nullable=False,
    )
    metadata_nonce: Mapped[str] = mapped_column(
        Text, nullable=False,
    )
    metadata_key_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    workflow_name: str = ""
    description: str = ""
    domain:        Mapped[str] = mapped_column(Text, nullable=False, server_default="public")
    status:        Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    active_major:  Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    active_sub:    Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tags: list
    created_at:    Mapped[datetime] = _ts()
    updated_at:    Mapped[datetime] = _ts()
    deleted_at:    Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('draft','published','archived')",
                        name="ck_workflows_status"),
        Index("ix_workflows_tenant_updated", "tenant_id", "updated_at",
              postgresql_where=(deleted_at.is_(None))),
        Index("ix_workflows_tenant_creator", "tenant_id", "creator_user_id",
              postgresql_where=(deleted_at.is_(None))),
    )


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    __allow_unmapped__ = True
    wf_id:        Mapped[str] = mapped_column(
        Text, ForeignKey("workflows.wf_id", ondelete="RESTRICT"), primary_key=True)
    major:        Mapped[int] = mapped_column(Integer, primary_key=True)
    sub:          Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_major: Mapped[int | None] = mapped_column(Integer)
    parent_sub:   Mapped[int | None] = mapped_column(Integer)
    workflow_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_key_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    note_ciphertext: Mapped[str] = mapped_column(
        Text, nullable=False,
    )
    note_nonce: Mapped[str] = mapped_column(
        Text, nullable=False,
    )
    note_key_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    note: str = ""
    creator_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    ts:           Mapped[datetime] = _ts()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"))
    __table_args__ = (
        Index("ix_versions_wf_major", "wf_id", "major"),
    )


class WorkflowRunState(Base):
    """Lightweight control plane for interactive workflow-page execution.

    Large node inputs/outputs stay in the workflow run VFS. This table stores
    only the current UI-restorable state for one workflow.
    """

    __tablename__ = "workflow_run_state"
    wf_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workflows.wf_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    creator_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False,
    )
    turn_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    run_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="workflow")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    target_node_id: Mapped[str | None] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    private_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    private_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    started_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "run_kind IN ('workflow','node')",
            name="ck_workflow_run_state_kind",
        ),
        CheckConstraint(
            "status IN ('pending','running','success','stopped','error')",
            name="ck_workflow_run_state_status",
        ),
        Index("ix_workflow_run_state_status", "tenant_id", "status"),
    )

    @property
    def node_states(self) -> dict:
        return dict(getattr(self, "_private_node_states", {}) or {})

    @node_states.setter
    def node_states(self, value: dict) -> None:
        self._private_node_states = dict(value or {})

    @property
    def error(self) -> str | None:
        value = getattr(self, "_private_error", None)
        return str(value) if value is not None else None

    @error.setter
    def error(self, value: str | None) -> None:
        self._private_error = str(value) if value is not None else None


class WorkflowRunEvent(Base):
    """Ordered frontend replay events for the current workflow-page run."""

    __tablename__ = "workflow_run_events"
    wf_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workflow_run_state.wf_id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    payload_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    payload_key_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (
        Index("ix_workflow_run_events_wf_seq", "wf_id", "seq"),
    )

    @property
    def payload(self) -> dict:
        return dict(getattr(self, "_materialized_payload", {}) or {})

    @payload.setter
    def payload(self, value: dict) -> None:
        self._materialized_payload = dict(value or {})


class Chat(Base):
    __tablename__ = "chats"
    __allow_unmapped__ = True
    chat_id:         Mapped[str] = mapped_column(Text, primary_key=True)
    scope_id:        Mapped[str] = mapped_column(Text, nullable=False)
    major_version:   Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    creator_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    metadata_ciphertext: Mapped[str] = mapped_column(
        Text, nullable=False,
    )
    metadata_nonce: Mapped[str] = mapped_column(
        Text, nullable=False,
    )
    metadata_key_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: str = ""
    surface:         Mapped[str] = mapped_column(Text, nullable=False, server_default="chat")
    # The Agent SDK/runtime is selected once, when the Chat first starts. A
    # later change to the user's global default affects new chats only.
    runtime_type: Mapped[str | None] = mapped_column(Text)
    runtime_session_id: Mapped[str | None] = mapped_column(Text)
    runtime_state_ref: Mapped[str | None] = mapped_column(Text)
    runtime_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Last explicitly resolved model option for this Chat.  An omitted model
    # on a later Turn means "keep this Chat's selection", not "re-evaluate the
    # user's current global default".  That distinction prevents a refreshed
    # composer from silently switching a persisted Codex thread between the
    # ChatGPT-account and API-broker transports.
    runtime_model_id: Mapped[str | None] = mapped_column(Text)
    # Non-secret model/generation choices last accepted for this Chat. They are
    # mutable only between Turns and seed Resume; each AgentRun retains the
    # immutable model/source/effort snapshot used by that historical Turn.
    runtime_agent_settings: Mapped[dict | None] = mapped_column(JSONB)
    # Exact non-secret Runtime connection identity. It is fixed by the first
    # accepted Turn so provider-native history, credentials, and billing never
    # cross accounts; models and reasoning remain mutable within the connection.
    runtime_connection_id: Mapped[str | None] = mapped_column(Text)
    # Fixed when a Chat binds to its Runtime on the first Turn. These values are
    # deliberately immutable afterwards: regenerating a wall clock value for
    # every resume would invalidate the model provider's prompt-prefix cache.
    runtime_timezone: Mapped[str | None] = mapped_column(Text)
    runtime_started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    # Optimistic-concurrency revision for the Chat's selected custom MCP set.
    # Every user turn carries the revision it rendered; stale tabs cannot
    # silently overwrite a newer selection.
    mcp_config_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0",
    )
    # Per-chat metadata blob (mirrors ``ChatMessage.meta``). Holds the `/command`
    # mode system's ``{"active_modes": [...]}`` (sticky across turns/reopen) and
    # is free to carry future per-chat metadata. See migration 027.
    meta: dict
    # Browser side-panel V1: durable control-flow state only. Browser topology
    # (profile/window/panel ids and current tabs) remains inside the extension
    # and is observed by tools when needed; it is not duplicated in Agent state.
    browser_control_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="inactive")
    browser_session_id: Mapped[str | None] = mapped_column(Text)
    browser_session_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    browser_last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    browser_lost_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"))
    last_message_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at:      Mapped[datetime] = _ts()
    updated_at:      Mapped[datetime] = _ts()
    deleted_at:      Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    __table_args__ = (
        CheckConstraint("major_version > 0", name="ck_chats_major_pos"),
        CheckConstraint("surface IN ('chat','browser')",
                        name="ck_chats_surface"),
        CheckConstraint(
            "runtime_type IS NULL OR runtime_type ~ '^[a-z][a-z0-9_-]{0,63}$'",
            name="ck_chats_runtime_type",
        ),
        CheckConstraint("runtime_version > 0", name="ck_chats_runtime_version_pos"),
        CheckConstraint(
            "browser_control_status IN ('inactive','attaching','attached','lost')",
            name="ck_chats_browser_control_status",
        ),
        Index("ix_chats_scope_last_msg", "scope_id", "last_message_at",
              postgresql_where=(deleted_at.is_(None))),
        Index("ix_chats_surface_scope_last_msg", "surface", "scope_id", "last_message_at",
              postgresql_where=(deleted_at.is_(None))),
        Index(
            "uq_chats_active_browser_lease",
            "tenant_id",
            "creator_user_id",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL AND browser_control_status "
                "IN ('attaching','attached','lost')"
            ),
        ),
    )


class ChatMcpBinding(Base):
    """Durable current custom-MCP selection for one Chat."""

    __tablename__ = "chat_mcp_bindings"
    chat_id: Mapped[str] = mapped_column(
        Text, ForeignKey("chats.chat_id", ondelete="CASCADE"), primary_key=True,
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (
        Index("ix_chat_mcp_bindings_server", "mcp_server_id"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id:        Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Runtime-neutral stable id from the product event stream. Database ``id``
    # is ordering/storage identity; message_id is replay/idempotency identity.
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    chat_id:   Mapped[str] = mapped_column(
        Text, ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False)
    turn_id:   Mapped[str | None] = mapped_column(Text)
    role:      Mapped[str] = mapped_column(Text, nullable=False)
    content_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    content_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    content_key_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    ts:        Mapped[datetime] = _ts()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"))
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_chat_messages_message_id"),
        Index("ix_messages_chat_ts", "chat_id", "ts"),
        Index("ix_messages_turn", "turn_id",
              postgresql_where=(turn_id.isnot(None))),
    )


class Template(Base):
    __tablename__ = "templates"
    template_id:   Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"))
    creator_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    node_type:     Mapped[str] = mapped_column(Text, nullable=False)
    visibility:    Mapped[str] = mapped_column(Text, nullable=False, server_default="private")
    function_type: Mapped[dict | None] = mapped_column(JSONB)
    description:   Mapped[dict | None] = mapped_column(JSONB)
    agent_hint:    Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    display:       Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    workflow:      Mapped[dict] = mapped_column(JSONB, nullable=False)
    tags:          Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    private_ciphertext: Mapped[str | None] = mapped_column(Text)
    private_nonce: Mapped[str | None] = mapped_column(Text)
    private_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
    )
    version:       Mapped[str] = mapped_column(Text, nullable=False, server_default="1.0.0")
    preview_path:  Mapped[str | None] = mapped_column(Text)
    created_at:    Mapped[datetime] = _ts()
    updated_at:    Mapped[datetime] = _ts()
    deleted_at:    Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    __table_args__ = (
        CheckConstraint("visibility IN ('private','public')",
                        name="ck_tpl_visibility"),
        Index("ix_templates_node_type", "node_type",
              postgresql_where=(deleted_at.is_(None))),
    )


# ---------------------------------------------------------------------------
# Authentication and multi-tenant tables.
# These 5 tables are RLS-free; row-level security applies only to the
# business tables above, which carry a tenant_id FK into `tenants`.
# ---------------------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenants"
    tenant_id:  Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=func.gen_random_uuid())
    name:       Mapped[str] = mapped_column(Text, nullable=False)
    max_concurrent_deployments: Mapped[int | None] = mapped_column(
        Integer, nullable=True)
    created_at: Mapped[datetime] = _ts()


class User(Base):
    __tablename__ = "users"
    user_id:      Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=func.gen_random_uuid())
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    # Legacy physical columns remain fixed non-sensitive sentinels so old
    # structural SQL fixtures cannot bypass encrypted application writes.
    email_sentinel: Mapped[str] = mapped_column(
        "email", Text, nullable=False, unique=True
    )
    display_name_sentinel: Mapped[str] = mapped_column(
        "display_name", Text, nullable=False, server_default=""
    )
    profile_ciphertext: Mapped[str | None] = mapped_column(Text)
    profile_nonce: Mapped[str | None] = mapped_column(Text)
    profile_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
    )
    profile_email_lookup_hash: Mapped[str | None] = mapped_column(Text)
    status:       Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at:   Mapped[datetime] = _ts()
    updated_at:   Mapped[datetime] = _ts()
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','disabled','pending_deletion')",
            name="ck_users_status",
        ),
        Index("ix_users_profile_email_lookup", "profile_email_lookup_hash"),
    )


class UserAgentPreference(Base):
    """Backend-owned defaults used when a new Chat binds its runtime."""

    __tablename__ = "user_agent_preferences"
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    default_runtime_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="codex"
    )
    codex_managed_profile_id: Mapped[str | None] = mapped_column(Text)
    # IANA timezone used for Agent time context and as the default UI display
    # zone on every browser.  Nullable keeps pre-migration users distinguishable
    # from users who explicitly selected UTC.
    preferred_timezone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (
        CheckConstraint(
            "default_runtime_type ~ '^[a-z][a-z0-9_-]{0,63}$'",
            name="ck_user_agent_preferences_runtime_type",
        ),
    )


class AccountDeletionRequest(Base):
    __tablename__ = "account_deletion_requests"
    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False)
    email_snapshot_sentinel: Mapped[str] = mapped_column(
        "email_snapshot", Text, nullable=False, server_default=""
    )
    email_snapshot_ciphertext: Mapped[str | None] = mapped_column(Text)
    email_snapshot_nonce: Mapped[str | None] = mapped_column(Text)
    email_snapshot_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
    )
    deletion_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="immediate"
    )
    frozen_resource_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    requested_at: Mapped[datetime] = _ts()
    purge_after: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    purging_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','cancelled','purging','purged','failed')",
            name="ck_account_deletion_requests_status",
        ),
        CheckConstraint(
            "deletion_mode IN ('immediate','delayed')",
            name="ck_account_deletion_requests_mode",
        ),
        Index("ix_account_deletion_due", "status", "purge_after"),
    )


class AuthIdentity(Base):
    __tablename__ = "auth_identities"
    identity_id:  Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=func.gen_random_uuid())
    user_id:      Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    provider:     Mapped[str] = mapped_column(Text, nullable=False)
    provider_uid_sentinel: Mapped[str] = mapped_column(
        "provider_uid", Text, nullable=False
    )
    provider_uid_lookup_hash: Mapped[str] = mapped_column(Text, nullable=False)
    provider_uid_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    provider_uid_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    provider_uid_key_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    secret:       Mapped[str | None] = mapped_column(Text)
    created_at:   Mapped[datetime] = _ts()
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_uid_lookup_hash",
            name="uq_identity_provider_lookup",
        ),
        Index("ix_identities_user", "user_id"),
    )


class Session(Base):
    __tablename__ = "sessions"
    token_hash:   Mapped[str] = mapped_column(Text, primary_key=True)
    session_id:   Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, unique=True,
        server_default=func.gen_random_uuid())
    user_id:      Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False)
    # ``tenant_id`` remains the physical compatibility column while business
    # code migrates to the explicit active-organization name. Both are changed
    # atomically by AuthRepo.switch_active_organization().
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    active_organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("organizations.tenant_id"),
        nullable=False)
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="1")
    authentication_strength: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="password")
    # Step-up is deliberately short lived even though the parent Session may
    # last for days. High-risk dependencies require phishing-resistant
    # WebAuthn strength and a future expiry, so a stale browser cannot retain
    # elevated authority.
    step_up_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    # Present only for a short-lived privileged-support Session. It is not a
    # wildcard role: every request revalidates the referenced explicit scope.
    privileged_access_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "privileged_access_requests.request_id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_sessions_privileged_access_request",
        ),
        nullable=True,
    )
    # Browser sessions are ambient credentials and therefore carry an
    # explicit audience.  An extension iframe receives a derived session via
    # one-time exchange; the primary Web token is never copied into extension
    # storage.  Deleting the parent Web session cascades to every derivative.
    audience: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="web")
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=True,
    )
    # Hash of the non-HttpOnly double-submit value.  Binding it to the Session
    # prevents an attacker-controlled cookie from becoming a valid CSRF token.
    csrf_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime] = _ts()
    expires_at:   Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = _ts()
    __table_args__ = (
        Index("ix_sessions_user", "user_id"),
        Index("ix_sessions_expires", "expires_at"),
        CheckConstraint("generation > 0", name="ck_sessions_generation"),
        CheckConstraint(
            "tenant_id = active_organization_id",
            name="ck_sessions_active_organization_matches_tenant",
        ),
        CheckConstraint(
            "authentication_strength IN "
            "('password','oauth','webauthn')",
            name="ck_sessions_authentication_strength",
        ),
        CheckConstraint(
            "audience IN ('web','extension','api','support')",
            name="ck_sessions_audience",
        ),
        CheckConstraint(
            "(audience = 'support') = "
            "(privileged_access_request_id IS NOT NULL)",
            name="ck_sessions_support_scope",
        ),
    )


class UserWebAuthnCredential(Base):
    """Account-global phishing-resistant WebAuthn credential metadata."""

    __tablename__ = "user_webauthn_credentials"
    credential_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0",
    )
    transports: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}",
    )
    device_type: Mapped[str] = mapped_column(Text, nullable=False)
    backed_up: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
    )
    name: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="Security key",
    )
    created_at: Mapped[datetime] = _ts()
    last_used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
    )
    __table_args__ = (
        Index("ix_user_webauthn_credentials_user", "user_id"),
        Index("ix_user_webauthn_credentials_tenant", "tenant_id"),
    )


class UserWebAuthnChallenge(Base):
    """One-time, Session-bound WebAuthn ceremony challenge."""

    __tablename__ = "user_webauthn_challenges"
    challenge_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    challenge: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
    )
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('registration','authentication')",
            name="ck_user_webauthn_challenges_purpose",
        ),
        UniqueConstraint(
            "session_id", "purpose",
            name="uq_user_webauthn_challenge_session_purpose",
        ),
        Index("ix_user_webauthn_challenges_expires", "expires_at"),
    )


class SessionExchangeCode(Base):
    """Single-use handoff from a primary Web Session to an extension iframe."""

    __tablename__ = "session_exchange_codes"
    code_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    audience: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="extension")
    created_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False)
    __table_args__ = (
        Index("ix_session_exchange_codes_expires", "expires_at"),
        CheckConstraint(
            "audience = 'extension'",
            name="ck_session_exchange_codes_audience",
        ),
    )


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id:    Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False)
    used_at:    Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


# ---------------------------------------------------------------------------
# Append-only audit log with forced RLS and nullable tenant ownership.
#
# The table is built by migration 001's create_all (this model); migration 009
# adds the security layer (FORCE RLS + split SELECT/INSERT policies + the
# append-only BEFORE UPDATE OR DELETE trigger + the action CHECK + the
# tenant_id auto-fill default). The model keeps ONLY the outcome CHECK; the
# action CHECK lives in 009 so the taxonomy has a single SQL home and
# audit.actions.AUDIT_ACTIONS stays the Python source of truth.
# ---------------------------------------------------------------------------


class AuditLog(Base):
    __tablename__ = "audit_log"
    audit_id:      Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=func.gen_random_uuid())
    # ON DELETE SET NULL (spec D5/§4): compliance rows MUST outlive their
    # referents. NO ACTION would block tenant/user deletion; CASCADE would
    # destroy the trail. SET NULL keeps the row; encrypted private snapshots
    # preserve authorized readability. tenant_id is nullable so unknown-email
    # auth failures (no tenant) can still be minimized and recorded.
    # server_default=FetchedValue(): the DB-side default
    # (current_setting('app.tenant_id')::uuid) is created in migration 009, not
    # the model. FetchedValue tells the ORM to OMIT this column on INSERT when
    # unset (so the resource-path add_row lets the GUC default fill it) and to
    # fetch the generated value back — without it SQLAlchemy emits an explicit
    # NULL, which the RLS INSERT policy rejects.
    tenant_id:     Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="SET NULL"), nullable=True,
        server_default=FetchedValue())
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    actor_email:   Mapped[str | None] = mapped_column(Text)
    action:        Mapped[str] = mapped_column(Text, nullable=False)
    target_type:   Mapped[str | None] = mapped_column(Text)
    target_id:     Mapped[str | None] = mapped_column(Text)
    target_name:   Mapped[str | None] = mapped_column(Text)
    outcome:       Mapped[str] = mapped_column(Text, nullable=False)
    ip_address:    Mapped[str | None] = mapped_column(Text)
    user_agent:    Mapped[str | None] = mapped_column(Text)
    request_id:    Mapped[str | None] = mapped_column(Text)
    meta:          Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}")
    actor_lookup_hash: Mapped[str | None] = mapped_column(Text)
    ip_lookup_hash: Mapped[str | None] = mapped_column(Text)
    private_ciphertext: Mapped[str | None] = mapped_column(Text)
    private_nonce: Mapped[str | None] = mapped_column(Text)
    private_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
    )
    created_at:    Mapped[datetime] = _ts()
    __table_args__ = (
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_tenant_action", "tenant_id", "action"),
        CheckConstraint("outcome in ('success','failure')", name="ck_audit_outcome"),
    )


# ---------------------------------------------------------------------------
# VFS 2b-1 — durable artifact/scratch store (Postgres = source of record).
#
# Both tables use a tenant_id server default supplied by the request-scoped
# (the GUC default lives in migration 012, not the model) + FK → tenants CASCADE
# + FORCE RLS + a single FOR ALL policy. ``scope_id`` is a text namespace:
# real workflow ids use the workflow id, while chat/browser workspaces use
# internal workspace ids without requiring a row in ``workflows``. Built by migration
# 001's create_all; migration 012 adds ONLY the RLS layer.
# ---------------------------------------------------------------------------


class VfsArtifact(Base):
    """VFS 2b-1 — scope-scoped artifact (/files /data /exec). Source of record
    in Postgres. The scope id may be a real workflow id or an internal chat
    workspace id. tenant_id FetchedValue() + GUC default (migration 012) + FORCE
    RLS policy."""
    __tablename__ = "vfs_artifacts"
    scope_id:     Mapped[str] = mapped_column(Text, primary_key=True)
    path:         Mapped[str] = mapped_column(Text, primary_key=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="text")
    object_key:   Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract:     Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    abstract_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_nonce: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=True,
    )
    size_bytes:   Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Content identity is deliberately separate from ``last_access``. Reads may
    # touch the latter for LRU/accounting purposes, but must never look like a
    # file-content change to optimistic writes or live Preview subscribers.
    content_revision: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("gen_random_uuid()::text")
    )
    wf_version:   Mapped[str | None] = mapped_column(Text)
    created_at:   Mapped[datetime] = _ts()
    last_access:  Mapped[datetime] = _ts()
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False, server_default=FetchedValue())
    __table_args__ = (Index("ix_vfs_artifacts_access", "scope_id", "last_access"),)


class VfsScratch(Base):
    """VFS — scope-scoped scratch (/memory): the agent's working notes."""
    __tablename__ = "vfs_scratch"
    scope_id:     Mapped[str] = mapped_column(Text, primary_key=True)
    path:         Mapped[str] = mapped_column(Text, primary_key=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="text")
    abstract:     Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    abstract_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_nonce: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=True,
    )
    size_bytes:   Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    object_key:   Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime] = _ts()
    last_access:  Mapped[datetime] = _ts()
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False, server_default=FetchedValue())
    __table_args__ = (Index("ix_vfs_scratch_access", "scope_id", "last_access"),)


class VfsRun(Base):
    """RE-1 — run-scoped, ephemeral, binary-capable VFS tier (/run). Metadata ONLY;
    bytes live in the ObjectStore at object_key. Keyed by an explicit run_id (A0).
    tenant_id FetchedValue() + GUC default + FORCE RLS (mirrors VfsArtifact)."""
    __tablename__ = "vfs_run"
    run_id:       Mapped[str] = mapped_column(Text, primary_key=True)
    path:         Mapped[str] = mapped_column(Text, primary_key=True)
    object_key:   Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="application/octet-stream")
    size_bytes:   Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    content_revision: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("gen_random_uuid()::text")
    )
    abstract:     Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    abstract_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_nonce: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=True,
    )
    # UX-10e0 "keep latest run per workflow": which workflow this run belongs to.
    # NULLABLE — existing rows + /run-only runs with no wf keep NULL (and are NOT
    # matched by the per-workflow purge). Migration 024 adds the column + the
    # (tenant_id, wf_id) index that makes ``purge_workflow_runs`` cheap.
    wf_id:        Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime] = _ts()
    last_access:  Mapped[datetime] = _ts()
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False, server_default=FetchedValue())
    __table_args__ = (
        Index("ix_vfs_run_scope", "tenant_id", "run_id"),
        Index("ix_vfs_run_wf", "tenant_id", "wf_id"),
    )


class VfsArtifactEvent(Base):
    """Durable, cross-worker file-content change cursor for Preview.

    Rows are written by database triggers on ``vfs_artifacts`` and ``vfs_run``
    in the same transaction as the mutation. This makes replay after an API
    worker restart gap-free and keeps read-only ``last_access`` touches out of
    the event stream.
    """

    __tablename__ = "vfs_artifact_events"
    event_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_revision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('artifact','run')",
            name="ck_vfs_artifact_events_scope_kind",
        ),
        CheckConstraint(
            "event_type IN ('upsert','delete')",
            name="ck_vfs_artifact_events_type",
        ),
        Index(
            "ix_vfs_artifact_events_file_cursor",
            "tenant_id",
            "scope_kind",
            "scope_id",
            "path",
            "event_id",
        ),
    )


# updated_at auto-trigger DDL — applied in the Alembic initial migration
# (Task 3), NOT here. Kept as a constant so the migration imports it.
UPDATED_AT_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
"""

UPDATED_AT_TRIGGERS = [
    ("workflows", "trg_workflows_updated_at"),
    ("chats", "trg_chats_updated_at"),
    ("templates", "trg_templates_updated_at"),
]

# Import task models so Task and TaskEvent register on
# Base.metadata (and migration 001's create_all picks them up). Placed
# at the END of the file to avoid a circular import on Base.
from . import models_tasks  # noqa: F401,E402  -- side-effect: model registration

# Deployments — import so Deployment registers on Base.metadata
# (migration 001's create_all picks it up). See models_deployments.py.
from . import models_deployments  # noqa: F401,E402  -- side-effect: model registration

# MCP servers — import so McpServer registers on Base.metadata
# (migration 001's create_all picks it up). See models_mcp_servers.py.
from . import models_mcp_servers  # noqa: F401,E402  -- side-effect: model registration

# LLM credentials (API Management Center) — import so LlmCredential registers
# on Base.metadata (migration 001's create_all picks it up). See
# models_llm_credentials.py.
from . import models_llm_credentials  # noqa: F401,E402  -- side-effect: model registration

# KB / RAG — import so KnowledgeBase / KbFile / KbChunk register on
# Base.metadata (migration 001's create_all picks them up). See
# models_kb.py.
from . import models_kb  # noqa: F401,E402  -- side-effect: model registration

# Durable interactive agent turns + resumable UI event log.
from . import models_agent_runs  # noqa: F401,E402  -- side-effect: model registration
from . import models_background_jobs  # noqa: F401,E402  -- side-effect: model registration

# Organization identity — import so Organization/Group/OrgMembership/
# GroupMembership register on Base.metadata. RLS + indexes live in migrations.
from . import models_org  # noqa: F401,E402  -- side-effect: model registration

# Durable OpenFGA mutation intent + edge revisions.
from . import models_authorization  # noqa: F401,E402

# Non-interactive execution identities for Task/Schedule/Deployment roots.
from . import models_service_accounts  # noqa: F401,E402

# Host-side envelope encrypted secrets. Business rows retain only secret_ref.
from . import models_secrets  # noqa: F401,E402

# Durable account erasure state machine. Kept separate from authorization.
from . import models_purge  # noqa: F401,E402

# Per-resource envelope keys for private content (Chat/Workflow first wave).
from . import models_content_keys  # noqa: F401,E402

# Short-lived privileged support control plane. Never projected into OpenFGA.
from . import models_privileged_access  # noqa: F401,E402
from . import models_enterprise_identity  # noqa: F401,E402

# Skills — import so Skill + SkillFile register on Base.metadata. RLS + the
# partial-unique index live in migration 025. See models_skills.py.
from . import models_skills  # noqa: F401,E402  -- side-effect: model registration

# Env builds — import so EnvBuild registers on Base.metadata. GLOBAL,
# content-addressed overlay registry; NO tenant_id / NO RLS (deliberate
# exception). Table also self-created in migration 026. See models_env_builds.py.
from . import models_env_builds  # noqa: F401,E402  -- side-effect: model registration
