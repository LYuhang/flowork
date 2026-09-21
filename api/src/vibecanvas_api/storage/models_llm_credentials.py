"""LLM credentials — per-tenant private store of "bring your own key" LLM
configs managed through the API Management Center.

Mirrors ``models_mcp_servers.py`` (the secret-bearing, FORCE-RLS, soft-delete
template):

  - ``tenant_id`` FK + FORCE RLS (the at-rest tenant-isolation boundary)
  - ``user_id`` ownership column
  - soft delete via ``deleted_at``
  - partial UNIQUE ``(tenant_id, name) WHERE deleted_at IS NULL`` (migration)
  - API keys exist only behind ``secret_ref`` + ``secret_version``.

The table registers on the shared ``Base.metadata`` (``storage/models.py``'s
tail-import). Migration 001's ``Base.metadata.create_all(bind)`` reflects the
current models so it creates the table; migration 021 adds the RLS policies and
the partial unique index that ``create_all`` cannot express.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vibecanvas_api.storage.models import Base


class LlmCredential(Base):
    __tablename__ = "llm_credentials"

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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # Runtime ownership is explicit: a saved provider connection must never
    # appear in multiple Runtime catalogs merely because they speak a similar
    # wire protocol.
    runtime_scope: Mapped[str] = mapped_column(
        Text, nullable=False, default="codex", server_default=text("'codex'"),
    )
    connection_kind: Mapped[str] = mapped_column(
        Text, nullable=False, default="manual", server_default=text("'manual'"),
    )
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_context_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Provider-derived, non-secret metadata. API keys and OAuth verifier
    # material always remain in encrypted_secrets instead.
    model_catalog: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"),
    )
    catalog_refreshed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    catalog_error_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Optional HTTP/HTTPS proxy for this provider's outbound calls. May carry
    # ``user:pass@host`` — private (NEVER on the public list shape), like
    # api_url / api_key. Added in migration 023.
    proxy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    connection_secret_ref: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("encrypted_secrets.secret_id", ondelete="RESTRICT"),
        nullable=True,
    )
    connection_secret_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    secret_ref: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("encrypted_secrets.secret_id", ondelete="RESTRICT"),
        nullable=False,
    )
    secret_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"),
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
            "runtime_scope ~ '^[a-z][a-z0-9_-]{0,63}$'",
            name="ck_llm_credentials_runtime_scope",
        ),
        CheckConstraint(
            "connection_kind IN ('manual', 'openrouter_oauth')",
            name="ck_llm_credentials_connection_kind",
        ),
        CheckConstraint(
            "position('?' in coalesce(api_url,''))=0 AND "
            "position('?' in coalesce(proxy,''))=0 AND "
            "coalesce(api_url,'') !~ "
            "'^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*@' AND "
            "coalesce(proxy,'') !~ "
            "'^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*@'",
            name="ck_llm_connection_public_projection",
        ),
    )

    # The partial UNIQUE index on (tenant_id, name) WHERE deleted_at IS NULL and
    # the RLS policies live in migration 021.


class OpenRouterOauthState(Base):
    """Single-use, user-bound PKCE state. The verifier is envelope-encrypted."""

    __tablename__ = "openrouter_oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    state_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    verifier_secret_ref: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("encrypted_secrets.secret_id", ondelete="RESTRICT"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
