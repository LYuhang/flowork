"""LLM credential schemas for the API Management Center.

THE SECRECY CONTRACT. ``api_key`` (and the model_name / api_url config) are
private to the owner; they are surfaced through layered out-shapes so a "list"
or "picker" surface (PromptNode / agent — a LATER phase) NEVER sees secret
material:

  - ``CredentialPublicOut``  — id, name, description, provider, timestamps.
    The ONLY fields any list/public surface returns. NO model_name / api_url /
    api_key. (``provider`` IS public — it lets a picker show a provider badge.)
  - ``CredentialOwnerOut``   — the owner's management/edit view: adds
    model_name + api_url + an ``api_key_set: bool`` flag. STILL no plaintext key.

Keys are write-only at the API boundary and stored through the envelope-
encrypted SecretService; ordinary management APIs never reveal plaintext.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from vibecanvas_api.schemas.access import ResourceAccessOut

# ----------------------------------------------------------------- out shapes


class CredentialPublicOut(BaseModel):
    """Public / list view. NEVER includes model_name, api_url, or api_key."""

    id: str
    name: str
    description: Optional[str] = None
    provider: str
    connection_kind: Literal["manual", "openrouter_oauth"] = "manual"
    runtime_scope: str
    model_context_tokens: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    access: ResourceAccessOut | None = None


class CredentialOwnerOut(BaseModel):
    """Owner management / edit view. Exposes the non-secret config
    (model_name, api_url) plus an ``api_key_set`` flag — but NEVER the
    plaintext key."""

    id: str
    name: str
    description: Optional[str] = None
    provider: str
    connection_kind: Literal["manual", "openrouter_oauth"] = "manual"
    runtime_scope: str
    model_name: str
    model_context_tokens: Optional[int] = None
    api_url: Optional[str] = None
    # Optional outbound proxy. Owner-only (like api_url) — may carry
    # ``user:pass@host``, so it is NEVER on CredentialPublicOut.
    proxy: Optional[str] = None
    api_key_set: bool
    created_at: datetime
    updated_at: datetime
    access: ResourceAccessOut | None = None


class CredentialConnectionTestOut(BaseModel):
    """Sanitized connection-test result. Provider response bodies and secret
    material never cross the API boundary."""

    ok: bool
    outcome: Literal[
        "connected",
        "credentials_rejected",
        "endpoint_rejected",
        "unreachable",
    ]
    latency_ms: int
    upstream_status: Optional[int] = None


class OpenRouterModelOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str = ""
    context_length: int | None = None
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    supports_tools: bool = False
    supports_web_search: bool = False
    supported_reasoning_efforts: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None
    pricing: dict[str, str | None] = Field(default_factory=dict)
    available: bool = True


class OpenRouterConnectionOut(BaseModel):
    connected: bool
    credential_id: str | None = None
    models: list[OpenRouterModelOut] = Field(default_factory=list)
    catalog_refreshed_at: datetime | None = None
    catalog_stale: bool = False
    error_code: str | None = None


class OpenRouterStartOut(BaseModel):
    authorization_url: str
    expires_at: datetime


class OpenRouterCallbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4000)
    state: str = Field(min_length=32, max_length=512)


# ----------------------------------------------------------------- in shapes


class CredentialCreate(BaseModel):
    """Create body. ``extra='forbid'`` blocks a client smuggling
    tenant_id / user_id / id / enabled (G4b trust boundary)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    provider: str = Field(..., min_length=1, max_length=100)
    runtime_scope: str = Field(default="codex", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    model_name: str = Field(..., min_length=1, max_length=200)
    model_context_tokens: Optional[int] = Field(default=None, gt=0)
    api_url: Optional[str] = Field(default=None, max_length=2000)
    proxy: Optional[str] = Field(default=None, max_length=2000)
    api_key: str = Field(..., min_length=1, max_length=4000)


class CredentialUpdate(BaseModel):
    """Update body. All fields optional (partial update). ``api_key`` omitted
    OR empty => keep the existing key (the handler keys off
    ``exclude_unset=True`` + a non-empty check). ``extra='forbid'``."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    provider: Optional[str] = Field(default=None, min_length=1, max_length=100)
    runtime_scope: Optional[str] = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    model_name: Optional[str] = Field(
        default=None, min_length=1, max_length=200,
    )
    model_context_tokens: Optional[int] = Field(default=None, gt=0)
    api_url: Optional[str] = Field(default=None, max_length=2000)
    proxy: Optional[str] = Field(default=None, max_length=2000)
    # Empty string is allowed here (means "keep existing"); the handler treats
    # a falsy api_key as "no change".
    api_key: Optional[str] = Field(default=None, max_length=4000)
