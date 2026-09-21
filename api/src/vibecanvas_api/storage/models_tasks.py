"""Task tracking models for tasks and task events.

Both tables force RLS and use tenant policies.

These two tables register on the shared ``Base.metadata`` declared in
``storage/models.py``; migration 001's ``Base.metadata.create_all(bind)``
reflects the current models so it creates these tables alongside the
business tables. Migration 004 adds the RLS policy that
``create_all`` cannot express (ENABLE/FORCE + tenant policy).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, Float, ForeignKey, Index, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vibecanvas_api.storage.models import Base


class Task(Base):
    __tablename__ = "tasks"
    __allow_unmapped__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False,
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_accounts.service_account_id", ondelete="RESTRICT"),
        nullable=True,
    )
    workflow_id: Mapped[Optional[str]] = mapped_column(
        Text, ForeignKey("workflows.wf_id", ondelete="CASCADE"), nullable=True,
    )
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="queued", default="queued",
    )
    progress: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"), default=0.0,
    )
    content_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    content_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    content_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Materialized only by TasksRepo after authenticated decryption.
    payload: dict
    result: Optional[dict]
    error: Optional[str]
    results_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    background_job_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deployment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deployments.id", ondelete="SET NULL"),
        nullable=True,
    )
    cluster_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "task_type IN ('batch_exec', 'scheduled_run')",
            name="ck_tasks_task_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'finished', 'failed', "
            "'cancelling', 'cancelled', 'finished_with_errors', "
            "'interrupted', 'resuming', 'enabled', 'paused')",
            name="ck_tasks_status",
        ),
        CheckConstraint("progress >= 0 AND progress <= 1", name="ck_tasks_progress"),
        Index("ix_tasks_tenant_status", "tenant_id", "status", "submitted_at"),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4,
    )
    payload_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    payload_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    payload_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    payload: dict
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('state', 'progress', 'log', 'result', 'terminal')",
            name="ck_task_events_event_type",
        ),
        Index("ix_task_events_task_ts", "task_id", "ts"),
    )


class TaskSchedule(Base):
    __tablename__ = "task_schedules"
    __allow_unmapped__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False,
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_accounts.service_account_id", ondelete="RESTRICT"),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workflows.wf_id", ondelete="CASCADE"), nullable=False,
    )
    name: str = ""
    enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"), default=True,
    )
    schedule_type: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    interval_seconds: Mapped[Optional[int]] = mapped_column(nullable=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    private_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    private_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    private_schema_version: Mapped[int] = mapped_column(
        nullable=False, server_default=text("2"), default=2,
    )
    input_preset: dict
    notification_policy: dict
    mount_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), default=False,
    )
    concurrency_policy: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="skip_if_running", default="skip_if_running",
    )
    failure_policy: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="none", default="none",
    )
    catchup_policy: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), default=False,
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("schedule_type IN ('interval', 'cron')", name="ck_task_schedules_type"),
        CheckConstraint(
            "concurrency_policy IN ('skip_if_running')",
            name="ck_task_schedules_concurrency_policy",
        ),
        CheckConstraint("failure_policy IN ('none')", name="ck_task_schedules_failure_policy"),
        UniqueConstraint("task_id", name="uq_task_schedules_task_id"),
        Index("ix_task_schedules_due", "tenant_id", "enabled", "next_run_at"),
    )


class ScheduledRunExecution(Base):
    __tablename__ = "scheduled_run_executions"
    __allow_unmapped__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workflows.wf_id", ondelete="CASCADE"), nullable=False,
    )
    run_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued", default="queued")
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    private_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    private_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_encryption_keys.key_id", ondelete="RESTRICT"),
        nullable=False,
    )
    input_snapshot: dict
    result: Optional[dict]
    error: Optional[str]
    run_state: dict
    notification_state: dict
    results_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped')",
            name="ck_scheduled_run_executions_status",
        ),
        CheckConstraint(
            "trigger_type IN ('scheduled', 'manual')",
            name="ck_scheduled_run_executions_trigger_type",
        ),
        UniqueConstraint("schedule_id", "run_key", name="uq_scheduled_run_executions_run_key"),
        Index("ix_scheduled_run_executions_history", "tenant_id", "schedule_id", "triggered_at"),
    )
