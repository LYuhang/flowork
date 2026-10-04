"""Durable authorization mutation intent and per-edge revisions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from vibecanvas_api.storage.models import Base


class AuthzEdgeRevision(Base):
    __tablename__ = "authz_edge_revisions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    object_type: Mapped[str] = mapped_column(Text, primary_key=True)
    object_id: Mapped[str] = mapped_column(Text, primary_key=True)
    relation: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_type: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_id: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_relation: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        default="",
        server_default="",
    )
    current_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "current_revision > 0",
            name="ck_authz_edge_current_revision",
        ),
        CheckConstraint(
            "object_type IN ("
            "'organization','group','chat','workflow','template',"
            "'task','deployment','storage_root','knowledge_base',"
            "'mcp_installation','skill_installation',"
            "'llm_credential','service_account'"
            ")",
            name="ck_authz_edge_object_type",
        ),
        CheckConstraint(
            "subject_type IN ("
            "'user','service_account','group','organization'"
            ")",
            name="ck_authz_edge_subject_type",
        ),
    )


class AuthzMutation(Base):
    __tablename__ = "authz_mutations"

    mutation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    desired_state: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject_relation: Mapped[str | None] = mapped_column(Text)
    source_revision: Mapped[str | None] = mapped_column(Text)
    edge_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    supersedes_mutation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("authz_mutations.mutation_id", ondelete="SET NULL"),
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="requested",
        server_default="requested",
    )
    revocation_guard_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_authz_mutation_idempotency",
        ),
        CheckConstraint(
            "actor_type IN ('user','service_account','system')",
            name="ck_authz_mutation_actor_type",
        ),
        CheckConstraint(
            "kind IN ('structural_projection','direct_binding')",
            name="ck_authz_mutation_kind",
        ),
        CheckConstraint(
            "operation IN ('write','delete')",
            name="ck_authz_mutation_operation",
        ),
        CheckConstraint(
            "desired_state IN ('present','absent')",
            name="ck_authz_mutation_desired_state",
        ),
        CheckConstraint(
            "(operation = 'write' AND desired_state = 'present') "
            "OR (operation = 'delete' AND desired_state = 'absent')",
            name="ck_authz_mutation_operation_matches_desired",
        ),
        CheckConstraint(
            "object_type IN ("
            "'organization','group','chat','workflow','template',"
            "'task','deployment','storage_root','knowledge_base',"
            "'mcp_installation','skill_installation',"
            "'llm_credential','service_account'"
            ")",
            name="ck_authz_mutation_object_type",
        ),
        CheckConstraint(
            "subject_type IN ("
            "'user','service_account','group','organization'"
            ")",
            name="ck_authz_mutation_subject_type",
        ),
        CheckConstraint(
            "status IN ('requested','applied','failed','superseded')",
            name="ck_authz_mutation_status",
        ),
        CheckConstraint(
            "edge_revision > 0",
            name="ck_authz_mutation_edge_revision",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_authz_mutation_attempt_count",
        ),
        CheckConstraint(
            "(status = 'applied' AND applied_at IS NOT NULL) "
            "OR status <> 'applied'",
            name="ck_authz_mutation_applied_at",
        ),
        CheckConstraint(
            "NOT revocation_guard_active OR ("
            "desired_state = 'absent' "
            "AND status IN ('requested','failed')"
            ")",
            name="ck_authz_mutation_revocation_guard",
        ),
    )


class SharedResourceProjection(Base):
    """Recipient locator for an applied direct User grant.

    This row never grants access by itself. It lets an authenticated recipient
    locate the owning tenant so the request can re-check the authoritative
    OpenFGA tuple under that tenant's RLS context.
    """

    __tablename__ = "shared_resource_projections"

    owner_tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    resource_type: Mapped[str] = mapped_column(Text, primary_key=True)
    resource_id: Mapped[str] = mapped_column(Text, primary_key=True)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    relation: Mapped[str] = mapped_column(Text, primary_key=True)
    source_mutation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("authz_mutations.mutation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    edge_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('workflow','task','deployment','knowledge_base','skill_installation')",
            name="ck_shared_resource_projection_type",
        ),
        CheckConstraint(
            "relation IN ('viewer','editor','operator','manager')",
            name="ck_shared_resource_projection_relation",
        ),
        CheckConstraint(
            "edge_revision > 0",
            name="ck_shared_resource_projection_revision",
        ),
        Index(
            "ix_shared_resource_recipient",
            "recipient_user_id",
            "resource_type",
            "updated_at",
        ),
        Index(
            "ix_shared_resource_owner",
            "owner_tenant_id",
            "resource_type",
            "resource_id",
        ),
    )
