"""Deployments — workflow-as-service (API / Webhook).

Spec §4.1. FORCE RLS + tenant policy + soft delete + per-deployment
QPS limits. Both external trigger types funnel through a single table.

This table registers on the shared ``Base.metadata`` declared in
``storage/models.py``; migration 001's ``Base.metadata.create_all(bind)``
reflects the current models so it creates the table alongside the
application tables. Migration 005 adds RLS and partial
indexes that ``create_all`` cannot express.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Text,
    func, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vibecanvas_api.storage.models import Base


class Deployment(Base):
    __tablename__ = "deployments"

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
    # Historical creator/steward projection. Authorization is exclusively
    # decided by OpenFGA and never inferred from this audit metadata.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False,
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_accounts.service_account_id", ondelete="RESTRICT"),
        nullable=True,
    )
    wf_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workflows.wf_id", ondelete="RESTRICT"), nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    version_pin: Mapped[str] = mapped_column(Text, nullable=False)
    pinned_major: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pinned_sub: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_millis: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("500"))
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("256"))
    worker_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    worker_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("-1"))
    api_key_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hmac_secret_ref: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("encrypted_secrets.secret_id", ondelete="RESTRICT"),
        nullable=True,
    )
    hmac_secret_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    rate_limit_qps: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
    )
    mount_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
    )
    active_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    rollout_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    rollout_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_invoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    invoke_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"),
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('api', 'webhook')",
            name="ck_deployments_trigger_type",
        ),
        CheckConstraint(
            "version_pin IN ('head', 'major', 'specific')",
            name="ck_deployments_version_pin",
        ),
        CheckConstraint(
            "(version_pin = 'head') OR "
            "(version_pin = 'major' AND pinned_major IS NOT NULL AND pinned_sub IS NULL) OR "
            "(version_pin = 'specific' AND pinned_major IS NOT NULL AND pinned_sub IS NOT NULL)",
            name="ck_deployments_pinned_required",
        ),
        CheckConstraint(
            "(trigger_type != 'api') OR (api_key_hash IS NOT NULL)",
            name="ck_deployments_api_key_required",
        ),
        CheckConstraint(
            "(trigger_type != 'webhook') OR "
            "(hmac_secret_ref IS NOT NULL)",
            name="ck_deployments_hmac_required",
        ),
        CheckConstraint(
            "rate_limit_qps >= 0",
            name="ck_deployments_rate_limit_nonneg",
        ),
        # Global partial UNIQUE on slug WHERE deleted_at IS NULL (migration
        # 088) and the other partial indexes live in migrations — Alembic can
        # express them as raw DDL, but SQLAlchemy's __table_args__ cannot
        # produce partial indexes portably here without coupling to the
        # column object before it exists.
    )
