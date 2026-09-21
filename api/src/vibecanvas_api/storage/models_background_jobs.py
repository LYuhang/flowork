"""Durable control-plane state for asynchronous Agent tool jobs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base


ACTIVE_BACKGROUND_JOB_STATUSES = (
    "queued",
    "running",
    "cancelling",
)
TERMINAL_BACKGROUND_JOB_STATUSES = (
    "completed",
    "failed",
    "cancelled",
)


class ChatToolJob(Base):
    __tablename__ = "chat_tool_jobs"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    chat_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("chats.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    creator_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
    )
    runtime_type: Mapped[str] = mapped_column(Text, nullable=False)
    executor_type: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="queued",
    )
    progress_current: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    progress_total: Mapped[int | None] = mapped_column(Integer)
    private_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    private_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    event_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0",
    )
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery: Mapped["ChatToolJobDelivery | None"] = relationship(
        back_populates="job",
        uselist=False,
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(
            "runtime_type ~ '^[a-z][a-z0-9_-]{0,63}$'",
            name="ck_chat_tool_jobs_runtime_type",
        ),
        CheckConstraint(
            "status IN ('queued','running','cancelling',"
            "'completed','failed','cancelled')",
            name="ck_chat_tool_jobs_status",
        ),
        CheckConstraint(
            "progress_current >= 0 AND "
            "(progress_total IS NULL OR progress_total >= progress_current)",
            name="ck_chat_tool_jobs_progress",
        ),
        UniqueConstraint(
            "chat_id",
            "idempotency_key",
            name="uq_chat_tool_jobs_chat_idempotency",
        ),
        Index(
            "ix_chat_tool_jobs_chat_created",
            "tenant_id",
            "chat_id",
            "created_at",
        ),
        Index(
            "ix_chat_tool_jobs_dispatch",
            "tenant_id",
            "status",
            "lease_expires_at",
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
    progress_message = property(
        lambda self: str(self._get_private("progress_message", "") or ""),
        lambda self, value: self._set_private(
            "progress_message", str(value or "")
        ),
    )
    input_snapshot = property(
        lambda self: dict(self._get_private("input_snapshot", {}) or {}),
        lambda self, value: self._set_private(
            "input_snapshot", dict(value or {})
        ),
    )
    result_snapshot = property(
        lambda self: dict(self._get_private("result_snapshot", {}) or {}),
        lambda self, value: self._set_private(
            "result_snapshot", dict(value or {})
        ),
    )
    result_ref = property(
        lambda self: self._get_private("result_ref", None),
        lambda self, value: self._set_private(
            "result_ref", str(value) if value is not None else None
        ),
    )
    error_json = property(
        lambda self: dict(self._get_private("error_json", {}) or {}),
        lambda self, value: self._set_private("error_json", dict(value or {})),
    )
    # Executor-private recovery coordinates are encrypted with the user-facing
    # fields but are never projected to the frontend or model.
    execution_handle_json = property(
        lambda self: dict(
            self._get_private("execution_handle_json", {}) or {}
        ),
        lambda self, value: self._set_private(
            "execution_handle_json", dict(value or {})
        ),
    )


class ChatToolJobDelivery(Base):
    """Delivery ledger kept separate from background execution state.

    A row is inserted in the same transaction as the durable hidden result
    Turn. Its absence means that a terminal result is still pending delivery;
    no execution status is mutated by the delivery coordinator.
    """

    __tablename__ = "chat_tool_job_deliveries"

    job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("chat_tool_jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("current_setting('app.tenant_id', true)::uuid"),
    )
    chat_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("chats.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    delivery_batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    job: Mapped[ChatToolJob] = relationship(back_populates="delivery")

    __table_args__ = (
        Index(
            "ix_chat_tool_job_deliveries_chat_batch",
            "tenant_id",
            "chat_id",
            "delivery_batch_id",
        ),
    )


class ChatToolJobEvent(Base):
    __tablename__ = "chat_tool_job_events"

    job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("chat_tool_jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        nullable=False,
        unique=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
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
        Index("ix_chat_tool_job_events_job_seq", "job_id", "seq"),
    )

    @property
    def payload(self) -> dict:
        return dict(getattr(self, "_materialized_payload", {}) or {})

    @payload.setter
    def payload(self, value: dict) -> None:
        self._materialized_payload = dict(value or {})
