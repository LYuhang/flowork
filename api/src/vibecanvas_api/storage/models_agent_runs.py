"""Durable control-plane models for interactive agent turns."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


ACTIVE_AGENT_RUN_STATUSES = ("running", "waiting_approval", "cancel_requested")
TERMINAL_AGENT_RUN_STATUSES = ("completed", "cancelled", "failed")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    chat_id: Mapped[str] = mapped_column(
        Text, ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False,
    )
    creator_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False,
    )
    client_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    input_message_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    last_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    private_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    private_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','waiting_approval','cancel_requested',"
            "'completed','cancelled','failed')",
            name="ck_agent_runs_status",
        ),
        UniqueConstraint("chat_id", "client_request_id", name="uq_agent_runs_chat_request"),
        Index(
            "uq_agent_runs_one_active_chat",
            "chat_id",
            unique=True,
            postgresql_where=text(
                "status IN ('running','waiting_approval','cancel_requested')"
            ),
        ),
        Index("ix_agent_runs_tenant_chat_created", "tenant_id", "chat_id", "created_at"),
        Index("ix_agent_runs_tenant_status_heartbeat", "tenant_id", "status", "heartbeat_at"),
    )

    @property
    def input_snapshot(self) -> dict:
        return dict(getattr(self, "_private_input_snapshot", {}) or {})

    @input_snapshot.setter
    def input_snapshot(self, value: dict) -> None:
        self._private_input_snapshot = dict(value or {})

    @property
    def error_message(self) -> str | None:
        value = getattr(self, "_private_error_message", None)
        return str(value) if value is not None else None

    @error_message.setter
    def error_message(self, value: str | None) -> None:
        self._private_error_message = value

    @property
    def native_boundary(self) -> dict | None:
        value = getattr(self, "_private_native_boundary", None)
        return dict(value) if value is not None else None

    @native_boundary.setter
    def native_boundary(self, value: dict | None) -> None:
        self._private_native_boundary = dict(value) if value is not None else None


class AgentRunEvent(Base):
    __tablename__ = "agent_run_events"

    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agent_runs.run_id", ondelete="CASCADE"), primary_key=True,
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    payload_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    payload_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_agent_run_events_run_seq", "run_id", "seq"),
    )

    @property
    def payload(self) -> dict:
        return dict(getattr(self, "_materialized_payload", {}) or {})

    @payload.setter
    def payload(self, value: dict) -> None:
        self._materialized_payload = dict(value or {})


class HitlRequest(Base):
    __tablename__ = "hitl_requests"

    hitl_request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    chat_id: Mapped[str] = mapped_column(
        Text, ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False,
    )
    run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
    )
    artifact_id: Mapped[str | None] = mapped_column(Text)
    hitl_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    private_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    private_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_interacted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "hitl_type IN ('pre_tool_approval','post_tool_review','elicitation')",
            name="ck_hitl_requests_type",
        ),
        CheckConstraint(
            "status IN ('pending','approved','denied','submitted','cancelled','expired')",
            name="ck_hitl_requests_status",
        ),
        Index("ix_hitl_requests_chat_status", "tenant_id", "chat_id", "status", "created_at"),
        Index("ix_hitl_requests_run_status", "tenant_id", "run_id", "status", "created_at"),
        Index("ix_hitl_requests_artifact", "tenant_id", "artifact_id"),
        CheckConstraint(
            "run_id IS NOT NULL",
            name="ck_hitl_requests_exactly_one_owner",
        ),
    )

    def _get_private(self, name: str, default):
        return getattr(self, f"_private_{name}", default)

    def _set_private(self, name: str, value) -> None:
        setattr(self, f"_private_{name}", value)

    title = property(
        lambda self: str(self._get_private("title", "") or ""),
        lambda self, value: self._set_private("title", str(value or "")),
    )
    prompt_text = property(
        lambda self: str(self._get_private("prompt_text", "") or ""),
        lambda self, value: self._set_private("prompt_text", str(value or "")),
    )
    ui_payload_json = property(
        lambda self: dict(self._get_private("ui_payload_json", {}) or {}),
        lambda self, value: self._set_private("ui_payload_json", dict(value or {})),
    )
    agent_payload_json = property(
        lambda self: dict(self._get_private("agent_payload_json", {}) or {}),
        lambda self, value: self._set_private("agent_payload_json", dict(value or {})),
    )
    decision_payload_json = property(
        lambda self: dict(self._get_private("decision_payload_json", {}) or {}),
        lambda self, value: self._set_private("decision_payload_json", dict(value or {})),
    )
    runtime_correlation_json = property(
        lambda self: dict(self._get_private("runtime_correlation_json", {}) or {}),
        lambda self, value: self._set_private("runtime_correlation_json", dict(value or {})),
    )
    resume_payload_json = property(
        lambda self: dict(self._get_private("resume_payload_json", {}) or {}),
        lambda self, value: self._set_private("resume_payload_json", dict(value or {})),
    )
    interaction_result_json = property(
        lambda self: dict(self._get_private("interaction_result_json", {}) or {}),
        lambda self, value: self._set_private("interaction_result_json", dict(value or {})),
    )


class InteractiveArtifact(Base):
    __tablename__ = "interactive_artifacts"

    artifact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    chat_id: Mapped[str] = mapped_column(
        Text, ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False,
    )
    run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
    )
    hitl_request_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("hitl_requests.hitl_request_id", ondelete="SET NULL"),
    )
    component_type: Mapped[str] = mapped_column(Text, nullable=False)
    completion_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="render_only")
    private_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    private_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_interacted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    content_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "completion_mode IN ('render_only','wait_for_submit')",
            name="ck_interactive_artifacts_completion_mode",
        ),
        Index("ix_interactive_artifacts_chat_created", "tenant_id", "chat_id", "created_at"),
        Index("ix_interactive_artifacts_run_created", "tenant_id", "run_id", "created_at"),
    )

    def _get_private(self, name: str, default):
        return getattr(self, f"_private_{name}", default)

    def _set_private(self, name: str, value) -> None:
        setattr(self, f"_private_{name}", value)

    title = property(
        lambda self: str(self._get_private("title", "") or ""),
        lambda self, value: self._set_private("title", str(value or "")),
    )
    definition_json = property(
        lambda self: dict(self._get_private("definition_json", {}) or {}),
        lambda self, value: self._set_private("definition_json", dict(value or {})),
    )
    widget_state_json = property(
        lambda self: dict(self._get_private("widget_state_json", {}) or {}),
        lambda self, value: self._set_private("widget_state_json", dict(value or {})),
    )
    interaction_result_json = property(
        lambda self: dict(self._get_private("interaction_result_json", {}) or {}),
        lambda self, value: self._set_private("interaction_result_json", dict(value or {})),
    )
    artifact_ref = property(
        lambda self: self._get_private("artifact_ref", None),
        lambda self, value: self._set_private("artifact_ref", value),
    )
