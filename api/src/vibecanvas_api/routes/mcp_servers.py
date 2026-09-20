"""MCP servers — Settings UI CRUD routes. MCP T5 ships ``POST /api/v1/mcp-servers``
+ ``POST /api/v1/mcp-servers/test``.

Two endpoints, same input shape:

* ``POST /test``   — dry-run handshake (no DB write). Used by the Add
  wizard's "Test connection" button so users can verify their endpoint /
  auth before committing.
* ``POST ""``      — create + persist. Performs the same handshake, then
  pre-checks for tool-name conflicts against built-ins AND other servers
  in the tenant, applies the per-server tool cap, and inserts the row.
  Rows are accepted even when the server is unreachable (handshake
  status ``error: ...``) — the operator can fix the endpoint later via
  PATCH (MCP T6); the row's ``last_handshake_status`` records WHY it
  isn't currently usable.

Trust boundary (mirrors Deployments T4 / G4b): ``ConfigDict(extra='forbid')``
on the body schema means a client cannot smuggle ``tenant_id`` /
``user_id`` / ``id`` / ``enabled`` / ``last_*`` fields. The handler
derives those exclusively from ``AuthContext`` + the handshake result.

Route ORDERING is load-bearing: ``/test`` is registered BEFORE any
``/{server_id}`` catch-all so FastAPI's path-matcher dispatches the
literal string. MCP T6 adds GET/PATCH/DELETE under ``/{server_id}`` —
they MUST be appended after the routes in this file, never before.
"""
from __future__ import annotations

import copy
import html
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.audit import actions
from vibecanvas_api.audit.context import extract_request_audit_context
from vibecanvas_api.audit.service import record_audit
from vibecanvas_api.auth.deps import (
    AuthContext,
    current_user,
    require_recent_step_up,
    tenant_db,
)
from vibecanvas_api.authorization.dependencies import (
    authorize_resource,
    context_for_auth,
    get_authz_service,
    mutation_coordinator_for_request,
    principal_for_auth,
)
from vibecanvas_api.authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    resource_root_edges,
)
from vibecanvas_api.authorization.service import (
    AuthzService,
    batch_resource_decisions,
)
from vibecanvas_api.authorization.types import (
    Action,
    AuthorizedResource,
    ConsistencyPreference,
    Decision,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.schemas.access import access_from_decision
from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.services.mcp_config import ALLOWED_TRANSPORTS, build_connection_config
from vibecanvas_api.services.mcp_connection_secrets import (
    hydrate_connection_credentials,
    store_connection_credentials,
)
from vibecanvas_api.services.mcp_handshake import handshake_one
from vibecanvas_api.services.mcp_catalog import resolve_catalog_item, search_catalog
from vibecanvas_api.services.mcp_oauth import (
    begin_connection,
    complete_connection,
    delete_oauth_connection,
    delete_oauth_transaction,
    delete_oauth_transactions_for_server,
    disconnect,
    resolve_oauth_auth_config,
    state_hash,
)
from vibecanvas_api.services.resource_provenance import (
    ResourceProvenanceBuilder,
)
from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.storage.repo_mcp_servers import McpOAuthRepo, McpServersRepo
from vibecanvas_api.agents.tools import builtin_tool_names as _agent_builtin_tool_names


router = APIRouter(prefix="/api/v1/mcp-servers", tags=["mcp-servers"])


# --------------------------------------------------------------------- schemas


class AuthConfig(BaseModel):
    """Auth configuration block stored on ``mcp_servers.auth_config``.

    ``type='none'`` ⇒ no HTTP header; the token field is ignored.
    ``type='bearer'`` ⇒ ``Authorization: Bearer <token>`` per request.
    The token format is restricted to printable ASCII (no whitespace)
    because it ends up in an HTTP header — a literal newline / NUL would
    let a malicious paste smuggle an extra header (CRLF injection). The
    1024-char cap is a sanity bound; real MCP bearers are <200 chars.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    token: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in ("none", "bearer"):
            raise ValueError("auth_config.type must be 'none' or 'bearer'")
        return v

    @field_validator("token")
    @classmethod
    def _token_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if len(v) > 1024:
            raise ValueError("auth_config.token must be <= 1024 chars")
        # ASCII printable, no whitespace — protects against CRLF / NUL
        # injection when the token is interpolated into an HTTP header.
        for ch in v:
            if not (0x21 <= ord(ch) <= 0x7e):
                raise ValueError(
                    "auth_config.token must be printable ASCII with no "
                    "whitespace (0x21-0x7e)"
                )
        return v

    @model_validator(mode="after")
    def _token_required_for_bearer(self):
        if self.type == "bearer" and not self.token:
            raise ValueError(
                "auth_config.token is required when type='bearer'"
            )
        return self


_DESCRIPTION_SOURCES = frozenset({
    "registry",
    "server_metadata",
    "synthesized",
    "user_edited",
    "ai_generated",
    "fallback",
})


class CreateBody(BaseModel):
    """Body schema for ``POST /api/v1/mcp-servers`` AND ``POST /test``.

    ``ConfigDict(extra='forbid')`` rejects unknown fields with 422 — a
    client CANNOT smuggle ``tenant_id`` / ``user_id`` / ``id`` /
    ``enabled`` / ``last_*`` fields. Identity columns come ONLY from
    ``AuthContext``; the handshake snapshot columns are written from
    the handshake result. This is the same G4b trust boundary
    Deployments T4 enforces.

    ``tool_prefix`` matches the DB CHECK constraint
    ``^[a-z][a-z0-9_]{0,30}$`` (migration via the model declaration);
    re-validating here gives a friendlier 422 instead of a 500 on
    IntegrityError.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    tool_prefix: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,30}$")
    transport: str
    endpoint: str = Field(..., min_length=1)
    description: Optional[str] = Field(default=None, max_length=2000)
    connection_config: dict = Field(default_factory=dict)
    auth_config: AuthConfig

    @field_validator("transport")
    @classmethod
    def _transport(cls, v: str) -> str:
        if v not in ALLOWED_TRANSPORTS:
            raise ValueError(
                "transport must be one of: stdio, sse, streamable_http, http"
            )
        return v

    @model_validator(mode="after")
    def _connection_config_matches_transport(self):
        build_connection_config(
            transport=self.transport,
            endpoint=self.endpoint,
            auth_config=self.auth_config.model_dump(),
            connection_config=self.connection_config,
        )
        return self


class CatalogInstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["official", "smithery"]
    source_id: str = Field(..., min_length=1, max_length=300)


class OAuthStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    return_origin: str = Field(..., min_length=1, max_length=500)


# --------------------------------------------------------------------- helpers


def _builtin_tool_names() -> set[str]:
    """Names of all built-in tools — for the cross-server conflict
    pre-check. Built-in names NEVER contain ``__`` by convention; an MCP
    prefix is always ``{prefix}__{name}``, so a collision here is rare
    but possible (a built-in literally named ``prefix__foo``)."""
    return _agent_builtin_tool_names()


def _catalog_prefix(name: str, source_id: str, used: set[str]) -> str:
    raw = (source_id.rsplit("/", 1)[-1] or name).lower()
    raw = re.sub(r"mcp[-_]?server|[-_]?mcp", "", raw)
    base = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")[:24]
    if not base or not base[0].isalpha():
        base = f"mcp_{base or 'server'}"
    base = base[:31]
    if base not in used:
        return base
    for number in range(2, 1000):
        suffix = f"_{number}"
        candidate = f"{base[:31 - len(suffix)]}{suffix}"
        if candidate not in used:
            return candidate
    return f"mcp_{uuid.uuid4().hex[:12]}"


def _synthesize_description(
    *, name: str, tool_names: list[dict] | None, status_str: str,
) -> tuple[str, str]:
    """Build a stable fallback brief description for agent catalogs."""
    tools = tool_names or []
    if tools:
        names = [str(t.get("name") or "").strip() for t in tools[:4]]
        names = [n for n in names if n]
        descriptions = [
            str(t.get("description") or "").strip().rstrip(".")
            for t in tools[:3]
            if str(t.get("description") or "").strip()
        ]
        sample = ", ".join(names)
        if descriptions:
            return (
                f"{name} exposes {len(tools)} MCP tool(s), including {sample}. "
                f"Typical capabilities: {'; '.join(descriptions)}.",
                "synthesized",
            )
        return (
            f"{name} exposes {len(tools)} MCP tool(s), including {sample}.",
            "synthesized",
        )
    if status_str == "ok":
        return (
            f"{name} is an MCP server with no tools reported by the latest probe.",
            "fallback",
        )
    return (
        f"{name} is an MCP server registration. Its available tools are unknown until connection succeeds.",
        "fallback",
    )


_MASKED_SECRET = "***"


def _mask_url_query(url: str) -> str:
    """Preserve query names while removing every stored query value."""
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        query = urlencode([(key, _MASKED_SECRET) for key, _ in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except ValueError:
        return url


def _scrub_connection_config(config_value: object) -> object:
    if not isinstance(config_value, dict):
        return config_value
    scrubbed = copy.deepcopy(config_value)
    for key in ("headers", "env"):
        values = scrubbed.get(key)
        if isinstance(values, dict):
            scrubbed[key] = {
                str(name): _MASKED_SECRET if value not in (None, "") else value
                for name, value in values.items()
            }
    if isinstance(scrubbed.get("url"), str):
        scrubbed["url"] = _mask_url_query(scrubbed["url"])
    return scrubbed


def _restore_masked_connection(candidate: dict, existing: dict) -> dict:
    """Replace outbound mask sent back by the editor with stored values."""
    restored = copy.deepcopy(candidate)
    for key in ("headers", "env"):
        values = restored.get(key)
        old_values = existing.get(key)
        if not isinstance(values, dict) or not isinstance(old_values, dict):
            continue
        restored[key] = {
            name: old_values.get(name) if value == _MASKED_SECRET else value
            for name, value in values.items()
        }

    url = restored.get("url")
    old_url = existing.get("url")
    if isinstance(url, str) and isinstance(old_url, str):
        try:
            parts = urlsplit(url)
            old_query = dict(parse_qsl(urlsplit(old_url).query, keep_blank_values=True))
            query = [
                (name, old_query.get(name, value) if value == _MASKED_SECRET else value)
                for name, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
            restored["url"] = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
        except ValueError:
            pass
    return restored


def _restore_masked_url(candidate: str, existing: str) -> str:
    restored = _restore_masked_connection(
        {"url": candidate},
        {"url": existing},
    )
    return str(restored.get("url") or candidate)


def _scrub(row: dict) -> dict:
    """Outbound row sanitization for create response.

    Drops the bearer ``token`` (replaced with ``"***"`` when present so
    the client UI can show "auth: bearer (token set)" without ever
    seeing the value). Coerces UUID / datetime values to
    JSON-serializable strings so callers don't have to coerce.

    Mirrors ``routes.deployments._scrub_secret_fields`` — same shape, same
    contract: the API never re-leaks stored credential material.
    """
    out = dict(row)
    for key in (
        "auth_secret_ref",
        "auth_secret_version",
        "connection_secret_ref",
        "connection_secret_version",
    ):
        out.pop(key, None)
    auth = out.get("auth_config") or {}
    if isinstance(auth, dict) and auth.get("type") == "bearer":
        # Shallow copy — never mutate the input.
        scrubbed_auth = dict(auth)
        scrubbed_auth["token"] = "***"
        out["auth_config"] = scrubbed_auth
    out["connection_config"] = _scrub_connection_config(out.get("connection_config"))
    if isinstance(out.get("endpoint"), str):
        out["endpoint"] = _mask_url_query(out["endpoint"])
    for key in ("id", "tenant_id", "user_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at", "last_handshake_at",
                "description_generated_at", "deleted_at"):
        v = out.get(key)
        if v is not None and hasattr(v, "isoformat"):
            out[key] = v.isoformat()
    return out


async def _scrub_with_access(
    row: dict,
    provenance: ResourceProvenanceBuilder,
    decision: Decision | None = None,
) -> dict:
    out = _scrub(row)
    if decision is not None:
        out["access"] = access_from_decision(decision).model_dump(mode="json")
    out["provenance"] = (
        await provenance.build(
            creator_user_id=row.get("user_id"),
            origin_type=(
                "catalog_install" if row.get("source") else "created"
            ),
        )
    ).model_dump(mode="json")
    return out


def _mcp_resource(
    ctx: AuthContext,
    server_id: uuid.UUID | str,
) -> ResourceRef:
    return ResourceRef(
        ResourceType.MCP_INSTALLATION,
        str(server_id),
        getattr(ctx, "active_organization_id", None) or ctx.tenant_id,
    )


async def _authorize_mcp(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    server_id: uuid.UUID | str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = _mcp_resource(ctx, server_id)
    decision = await service.check(
        principal_for_auth(ctx),
        action,
        resource,
        context_for_auth(ctx, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="mcp server not found",
        )
    return AuthorizedResource(resource=resource, decision=decision)


async def _authorize_organization_create(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
) -> None:
    await authorize_resource(
        request=request,
        auth=ctx,
        service=service,
        resource=ResourceRef(
            ResourceType.ORGANIZATION,
            ctx.active_organization_id,
            ctx.active_organization_id,
        ),
        action=Action.CREATE,
    )


async def _rebind_request_organization(
    session: AsyncSession,
    ctx: AuthContext,
) -> None:
    organization_id = (
        getattr(ctx, "active_organization_id", None) or ctx.tenant_id
    )
    await session.execute(
        text("SELECT set_config('app.tenant_id', :organization_id, true)"),
        {"organization_id": organization_id},
    )


async def _commit_new_mcp_projection(
    *,
    request: Request,
    session: AsyncSession,
    ctx: AuthContext,
    service: AuthzService,
    server_id: uuid.UUID,
    operation: str,
) -> Decision:
    coordinator = mutation_coordinator_for_request(
        request,
        ctx.active_organization_id,
    )
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=ctx.user_id,
        before=frozenset(),
        after=resource_root_edges(
            organization_id=ctx.active_organization_id,
            object_type="mcp_installation",
            object_id=str(server_id),
            owner_relation="installer",
            owner_type="user",
            owner_id=ctx.user_id,
        ),
        operation_id=f"mcp-installation:{server_id}:{operation}",
        source="mcp-installation-create",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_request_organization(session, ctx)
    return (
        await _authorize_mcp(
            request=request,
            ctx=ctx,
            service=service,
            server_id=server_id,
            action=Action.VIEW,
        )
    ).decision


def _reject_unsafe_probe(result: dict) -> None:
    """Do not persist or re-enable a destination rejected by SSRF policy."""
    if result.get("security_rejected"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(result.get("status") or "unsafe MCP destination"),
        )


# ---------------------------------------------------------------------- /test
#
# Route order matters: ``/test`` MUST be declared BEFORE any future
# ``/{server_id}`` route (MCP T6) — FastAPI matches in registration
# order, and a path-parameter route would otherwise swallow the literal
# string ``test``. Spec §8.


@router.post("/test")
async def dry_run_handshake(
    body: CreateBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Dry-run handshake. Does NOT persist. Used by the Settings UI Add
    wizard's "Test connection" button.

    Returns ``{ok: True, tool_count, tool_names}`` on success, or
    ``{ok: False, error}`` on any handshake failure (timeout, auth
    rejected, transport error, etc.). The response shape is deliberately
    different from the create response — this endpoint never returns a
    server row, so the frontend treats it as a strict probe result.

    No DB write happens here; we don't even need the tenant-bound
    session. ``current_user`` still gates the route so anonymous callers
    can't use us as a free MCP-server port scanner.
    """
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    result = await handshake_one(
        prefix=body.tool_prefix,
        transport=body.transport,
        endpoint=body.endpoint,
        auth_config=body.auth_config.model_dump(),
        connection_config=body.connection_config,
        timeout_s=config.mcp.handshake_timeout_s,
        tenant_id=ctx.tenant_id,
    )
    _reject_unsafe_probe(result)
    if result["status"] == "ok":
        return {
            "ok": True,
            "tool_count": result["tool_count"],
            "tool_names": result["tool_names"],
        }
    return {"ok": False, "error": result["status"]}


# ---------------------------------------------------------------------- create


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    body: CreateBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Create an MCP server registration for the current tenant.

    Flow:
      1. Probe the endpoint via ``handshake_one`` (10s timeout from
         ``config.mcp.handshake_timeout_s``).
      2. If reachable (``status='ok'``):
         a. Conflict pre-check: any prefixed tool name (``{prefix}__{name}``)
            colliding with a built-in tool name OR another live + enabled
            server's tool names → 409. Catching this BEFORE insert avoids
            leaving a row that the loader would silently drop.
         b. Per-server tool cap: if ``tool_count > per_server_tool_cap``,
            409 (avoid blowing the agent prompt budget). The per-TENANT
            cap is enforced lazily at load time (loader skips overflow
            servers); we only gate per-server here.
      3. If unreachable (``status='error: ...'``): skip the conflict
         pre-check (we have no tool names to compare) and accept the
         row anyway. ``last_handshake_status`` records the failure so the
         UI can flag "unreachable — fix endpoint or auth"; the row is
         enabled so a later retry / PATCH-and-refresh can recover it
         without a recreate.
      4. ``McpServersRepo.insert`` writes the row under the tenant-bound
         session (RLS applies). The partial UNIQUE indexes
         ``(tenant_id, name)`` and ``(tenant_id, tool_prefix)`` (migration
         006) surface as IntegrityError → 409.

    Returns the row with secrets scrubbed (HTTP 201).
    """
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    repo = McpServersRepo(session)

    # 1. Handshake first — informs whether we can pre-check conflicts.
    auth_dict = body.auth_config.model_dump()
    result = await handshake_one(
        prefix=body.tool_prefix,
        transport=body.transport,
        endpoint=body.endpoint,
        auth_config=auth_dict,
        connection_config=body.connection_config,
        timeout_s=config.mcp.handshake_timeout_s,
        tenant_id=ctx.tenant_id,
    )
    _reject_unsafe_probe(result)
    status_str = result["status"]
    tool_count = result.get("tool_count")
    tool_names = result.get("tool_names")

    # 2. Conflict pre-check + per-server cap, ONLY when reachable.
    if status_str == "ok":
        # Per-server tool cap — refuse before we taint the tenant's
        # tool surface.
        if tool_count is not None and tool_count > config.mcp.per_server_tool_cap:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"server exports {tool_count} tools; per-server cap "
                    f"is {config.mcp.per_server_tool_cap}"
                ),
            )
        # Build the would-be prefixed names this server contributes.
        new_prefixed = {
            f"{body.tool_prefix}__{tn['name']}"
            for tn in (tool_names or [])
        }
        builtin = _builtin_tool_names()
        builtin_overlap = new_prefixed & builtin
        if builtin_overlap:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "tool name conflicts with built-in: "
                    f"{sorted(builtin_overlap)}"
                ),
            )
        # Other live + enabled servers in the SAME tenant. Repo is
        # tenant-bound (session has app.tenant_id set), so RLS scopes
        # this naturally. No exclude_id because no row exists yet.
        other_names = await repo.list_other_tool_names()
        other_overlap = new_prefixed & other_names
        if other_overlap:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "tool name conflicts with another MCP server: "
                    f"{sorted(other_overlap)}"
                ),
            )

    # 3. Encrypt bearer material before persisting the registration. The
    # handshake above used only the request-local plaintext projection.
    sid = uuid.uuid4()
    stored_auth = {"type": auth_dict.get("type", "none")}
    auth_secret_ref = None
    if auth_dict.get("type") == "bearer":
        auth_secret_ref = await secret_service().put_text(
            session,
            tenant_id=ctx.tenant_id,
            purpose="mcp_bearer_token",
            resource_type="mcp_installation",
            resource_id=sid,
            plaintext=str(auth_dict["token"]),
        )
    stored_endpoint, stored_connection, connection_secret_ref = (
        await store_connection_credentials(
            session,
            tenant_id=ctx.tenant_id,
            server_id=sid,
            endpoint=body.endpoint,
            connection_config=body.connection_config,
            version=1,
        )
    )

    # Build INSERT fields. Identity from ctx ONLY (G4b). Handshake
    # snapshot fields come from the probe result — including the
    # failed-status case where last_tool_count / last_tool_names stay
    # NULL and last_handshake_at stays NULL too (we only set the
    # timestamp when we have a definitive result; an unreachable probe
    # is a transient signal, not a successful poll).
    fields: dict = dict(
        id=sid,
        tenant_id=uuid.UUID(ctx.tenant_id),
        user_id=uuid.UUID(ctx.user_id),
        name=body.name,
        tool_prefix=body.tool_prefix,
        transport=body.transport,
        endpoint=stored_endpoint,
        description=(body.description or "").strip(),
        description_source="user_edited"
        if (body.description or "").strip()
        else "fallback",
        auth_config=stored_auth,
        auth_secret_ref=auth_secret_ref,
        auth_secret_version=1,
        connection_config=stored_connection,
        connection_secret_ref=connection_secret_ref,
        connection_secret_version=1,
        enabled=True,
        last_handshake_status=status_str,
    )
    if status_str == "ok":
        fields["last_tool_count"] = tool_count
        fields["last_tool_names"] = tool_names
        # last_handshake_at is set by the DB to now() at INSERT time?
        # No — the column is nullable with no default. Set it here on
        # success so the UI's "last successful handshake" timestamp
        # works without a separate refresh hit.
        fields["last_handshake_at"] = datetime.now(timezone.utc)
    # Unreachable: leave last_tool_count / last_tool_names / last_handshake_at
    # NULL. The status string captures WHY.
    if not fields["description"]:
        desc, desc_source = _synthesize_description(
            name=body.name,
            tool_names=tool_names,
            status_str=status_str,
        )
        fields["description"] = desc
        fields["description_source"] = desc_source

    try:
        await repo.insert(**fields)
        await session.flush()  # surface IntegrityError NOW, not at request end
    except IntegrityError as e:
        # Only the two partial UNIQUE indexes are a user-facing conflict.
        # Do not collapse unrelated FK/CHECK/NOT NULL failures into a bogus
        # "already exists" response: doing so hides schema/security cutover
        # regressions and makes operators debug the wrong problem.
        if getattr(e.orig, "sqlstate", None) != "23505":
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "name or tool_prefix already exists for this tenant"
            ),
        ) from e

    # Audit: never include the auth_config token in meta.
    await record_audit(
        session,
        action=actions.MCP_SERVER_CREATE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=actions.TARGET_MCP_SERVER,
        target_id=str(sid),
        target_name=body.name,
        outcome="success",
        audit_ctx=extract_request_audit_context(request) if request else None,
        meta={},
    )
    decision = await _commit_new_mcp_projection(
        request=request,
        session=session,
        ctx=ctx,
        service=service,
        server_id=sid,
        operation="create",
    )
    row = await repo.get(sid)
    return await _scrub_with_access(
        row,
        ResourceProvenanceBuilder(session),
        decision,
    )


# ----------------------------------------------- patch / list / get schemas
#
# MCP T6 — CRUD over an existing registration. The PATCH body deliberately
# only lists the MUTABLE columns; ``transport`` and ``tool_prefix`` are
# load-bearing identifiers (prefix appears in every prefixed tool name,
# transport pins the wire protocol) so they are immutable post-create.
# ``extra='forbid'`` makes Pydantic reject a body that smuggles them with
# 422 — the same G4b trust boundary as ``CreateBody``.


class PatchBody(BaseModel):
    """Body schema for ``PATCH /api/v1/mcp-servers/{server_id}``.

    Only mutable columns. ``transport`` / ``tool_prefix`` are absent on
    purpose — they identify the server in the prefixed-name surface and
    cannot change without breaking already-bound agent tool references.
    ``extra='forbid'`` turns a smuggled immutable field into a 422.

    All fields default to ``None`` so a partial body (e.g. just
    ``{"enabled": false}``) is valid; ``model_dump(exclude_unset=True)``
    in the handler distinguishes "client said null" from "client omitted".
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    endpoint: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = Field(default=None, max_length=2000)
    connection_config: Optional[dict] = None
    description_source: Optional[str] = None
    description_model_id: Optional[str] = Field(default=None, max_length=500)
    description_basis_hash: Optional[str] = Field(default=None, max_length=200)
    auth_config: Optional[AuthConfig] = None
    enabled: Optional[bool] = None

    @field_validator("description_source")
    @classmethod
    def _description_source(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in _DESCRIPTION_SOURCES:
            raise ValueError(
                "description_source must be one of: "
                + ", ".join(sorted(_DESCRIPTION_SOURCES))
            )
        return v

    @field_validator("auth_config")
    @classmethod
    def _auth_not_null(cls, v):
        """Reject ``{"auth_config": null}`` with 422.

        Schema allows OMITTING the field (auth unchanged) — ``None`` is
        the unset sentinel for ``exclude_unset=True``. But an EXPLICIT
        ``null`` from the client is meaningless: there is no
        "no-auth-config" state for an MCP server (use ``{"type":"none"}``
        instead). Without this validator, an explicit ``null`` would
        sneak past Pydantic, the handler would invoke ``handshake_one``
        with ``auth_config=None``, and ``_headers(None).get(...)`` deep
        in the handshake path would raise ``AttributeError`` → 500.
        Surface the error at the boundary instead.
        """
        if v is None:
            raise ValueError(
                "auth_config cannot be null; omit the field to keep "
                "current value"
            )
        return v


# ------------------------------------------------------------------ list / get
#
# IMPORTANT route ordering: list (``""``) and ``/{server_id}`` are
# registered AFTER ``/test`` (declared above) so FastAPI's path-matcher
# dispatches the literal string ``test`` to ``dry_run_handshake`` first
# instead of binding ``server_id="test"``. Spec §8.


@router.get("/catalog")
async def discover_mcp_servers(
    source: Literal["official", "smithery"] = "official",
    search: str = "",
    limit: int = Query(default=10, ge=1, le=100),
    ctx: AuthContext = Depends(current_user),
):
    """Search a fixed public MCP catalog and return a normalized result list."""
    try:
        return await search_catalog(source=source, search=search, limit=limit)
    except (TimeoutError, httpx.HTTPError, ValueError) as exc:
        reason = str(exc).strip() or type(exc).__name__
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MCP catalog is unavailable: {reason}",
        ) from exc


@router.get("/catalog/resolve")
async def resolve_mcp_server_candidate(
    source: Literal["official", "smithery"],
    source_id: str,
    ctx: AuthContext = Depends(current_user),
):
    """Resolve a catalog candidate to an installable endpoint/stdio config."""
    try:
        return await resolve_catalog_item(source=source, source_id=source_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (TimeoutError, httpx.HTTPError, ValueError) as exc:
        reason = str(exc).strip() or type(exc).__name__
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MCP catalog is unavailable: {reason}",
        ) from exc


@router.post("/catalog/install", status_code=status.HTTP_201_CREATED)
async def install_catalog_mcp_server(
    body: CatalogInstallBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Install a catalog definition without conflating install and OAuth.

    OAuth servers are saved disabled with ``connection_required``. No MCP
    runtime or sandbox is started until the user explicitly connects an
    account from the installed server's Connection tab.
    """
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    try:
        item = await resolve_catalog_item(source=body.source, source_id=body.source_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (TimeoutError, httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MCP catalog item could not be resolved: {exc}",
        ) from exc
    connection = item.get("connection")
    if not isinstance(connection, dict) or not connection.get("endpoint"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This catalog entry has no supported connection",
        )
    if item.get("auth_mode") != "oauth":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This catalog entry requires setup before installation",
        )

    repo = McpServersRepo(session)
    existing = await repo.list_for_user(ctx.user_id)
    duplicate = next(
        (
            row for row in existing
            if row.get("source") == body.source and row.get("source_id") == body.source_id
        ),
        None,
    )
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This MCP server is already installed",
        )
    prefix = _catalog_prefix(
        str(item.get("name") or body.source_id),
        body.source_id,
        {str(row["tool_prefix"]) for row in existing},
    )
    fields = {
        "tenant_id": uuid.UUID(ctx.tenant_id),
        "user_id": uuid.UUID(ctx.user_id),
        "name": str(item.get("name") or body.source_id)[:200],
        "tool_prefix": prefix,
        "transport": connection["transport"],
        "endpoint": connection["endpoint"],
        "description": str(item.get("description") or "")[:2000],
        "description_source": "registry" if item.get("description") else "fallback",
        "source": body.source,
        "source_id": body.source_id,
        "source_url": item.get("homepage"),
        "auth_mode": "oauth",
        "auth_metadata_url": item.get("auth_metadata_url"),
        "connection_status": "connection_required",
        "connection_config": connection.get("connection_config") or {},
        "auth_config": {"type": "none"},
        "enabled": False,
        "last_handshake_status": "account connection required",
    }
    try:
        server_id = await repo.insert(**fields)
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This MCP server is already installed",
        ) from exc
    await record_audit(
        session,
        action=actions.MCP_SERVER_CREATE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=actions.TARGET_MCP_SERVER,
        target_id=str(server_id),
        target_name=fields["name"],
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"source": body.source, "connection_status": "connection_required"},
    )
    decision = await _commit_new_mcp_projection(
        request=request,
        session=session,
        ctx=ctx,
        service=service,
        server_id=server_id,
        operation="catalog-install",
    )
    return await _scrub_with_access(
        await repo.get(server_id),
        ResourceProvenanceBuilder(session),
        decision,
    )


def _oauth_callback_page(*, origin: str, server_id: str, ok: bool, message: str) -> HTMLResponse:
    payload = json.dumps(
        {
            "type": "vibecanvas:mcp-oauth-complete",
            "serverId": server_id,
            "ok": ok,
            "message": message,
        },
        ensure_ascii=True,
    )
    origin_parts = urlsplit(origin)
    can_notify_opener = (
        origin_parts.scheme in {"http", "https"} and bool(origin_parts.netloc)
    )
    target_origin = json.dumps(origin, ensure_ascii=True)
    title = "Account connected" if ok else "Connection failed"
    body = (
        "You can close this window and return to Flowork."
        if ok else message
    )
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head>"
        "<body style='font-family:system-ui;padding:32px;color:#202124'>"
        f"<h1 style='font-size:20px'>{html.escape(title)}</h1><p>{html.escape(body)}</p>"
        + (
            "<script>"
            f"if(window.opener){{window.opener.postMessage({payload},{target_origin});window.close();}}"
            "</script>"
            if can_notify_opener else ""
        )
        + "</body></html>",
        headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"},
    )


@router.get("/oauth/client-metadata")
async def mcp_oauth_client_metadata():
    """Public OAuth client metadata document for authorization servers."""
    try:
        client_id = config.public_urls.absolute("api/v1/mcp-servers/oauth/client-metadata")
        callback_url = config.public_urls.absolute("api/v1/mcp-servers/oauth/callback")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {
        "client_id": client_id,
        "client_name": "Flowork",
        "redirect_uris": [callback_url],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


@router.get("/oauth/callback", response_class=HTMLResponse)
async def mcp_oauth_callback(
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
):
    """Public OAuth callback. A one-time state token supplies tenant scope."""
    if not state:
        return _oauth_callback_page(origin="null", server_id="", ok=False, message="Missing OAuth state")
    async with session_scope_admin() as session:
        transaction = await McpOAuthRepo(session).get_transaction(state_hash(state))
        if not transaction:
            return _oauth_callback_page(origin="null", server_id="", ok=False, message="OAuth session expired; start the connection again")
        origin = transaction["return_origin"]
        server_id = str(transaction["server_id"])
        # The callback is intentionally public, so the one-time state is the
        # only browser credential available here.  Revalidate the durable
        # ownership and active organization membership before exchanging the
        # provider code: a user may have been suspended, removed, or may have
        # deleted the installation while the OAuth window was open.
        eligible = bool(
            (
                await session.execute(
                    text(
                        "SELECT EXISTS ("
                        " SELECT 1 FROM org_memberships AS membership"
                        " JOIN mcp_servers AS server"
                        "   ON server.id = :server_id"
                        "  AND server.tenant_id = membership.tenant_id"
                        "  AND server.user_id = membership.user_id"
                        "  AND server.deleted_at IS NULL"
                        " WHERE membership.tenant_id = :tenant_id"
                        "   AND membership.user_id = :user_id"
                        "   AND membership.status = 'active'"
                        ")"
                    ),
                    {
                        "server_id": transaction["server_id"],
                        "tenant_id": transaction["tenant_id"],
                        "user_id": transaction["user_id"],
                    },
                )
            ).scalar_one()
        )
        if not eligible:
            await delete_oauth_transaction(session, transaction["state_hash"])
            await McpServersRepo(session).update(
                transaction["server_id"],
                connection_status="connection_failed",
                enabled=False,
                last_handshake_status=(
                    "OAuth cancelled because installation access changed"
                ),
            )
            return _oauth_callback_page(
                origin=origin,
                server_id=server_id,
                ok=False,
                message=(
                    "Installation access changed; start the connection again"
                ),
            )
        if error or not code:
            await McpServersRepo(session).update(
                transaction["server_id"],
                connection_status="connection_failed",
                enabled=False,
                last_handshake_status=f"OAuth denied: {error_description or error or 'authorization code missing'}",
            )
            await delete_oauth_transaction(session, transaction["state_hash"])
            return _oauth_callback_page(
                origin=origin, server_id=server_id, ok=False,
                message=error_description or error or "Authorization was not completed",
            )
        try:
            transaction, server = await complete_connection(session, state=state, code=code)
            server = await hydrate_connection_credentials(session, server)
            auth_config = await resolve_oauth_auth_config(session, server)
            result = await handshake_one(
                prefix=server["tool_prefix"],
                transport=server["transport"],
                endpoint=server["endpoint"],
                auth_config=auth_config or {"type": "none"},
                connection_config=server.get("connection_config") or {},
                timeout_s=config.mcp.handshake_timeout_s,
                tenant_id=str(transaction["tenant_id"]),
            )
            if result["status"] == "ok":
                await McpServersRepo(session).update(
                    server["id"],
                    connection_status="connected",
                    enabled=True,
                    last_handshake_at=datetime.now(timezone.utc),
                    last_handshake_status="ok",
                    last_tool_count=result.get("tool_count"),
                    last_tool_names=result.get("tool_names"),
                )
                return _oauth_callback_page(
                    origin=origin, server_id=server_id, ok=True,
                    message="Account connected",
                )
            await McpServersRepo(session).update(
                server["id"], connection_status="connection_failed", enabled=False,
                last_handshake_at=datetime.now(timezone.utc),
                last_handshake_status=result["status"],
            )
            return _oauth_callback_page(
                origin=origin, server_id=server_id, ok=False,
                message="Account was authorized, but the MCP server connection test failed",
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            await McpServersRepo(session).update(
                transaction["server_id"], connection_status="connection_failed",
                enabled=False, last_handshake_status=f"OAuth failed: {exc}",
            )
            await delete_oauth_transaction(session, transaction["state_hash"])
            return _oauth_callback_page(
                origin=origin, server_id=server_id, ok=False, message=str(exc),
            )


@router.get("")
async def list_mcp_servers(
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """List every live MCP server installed by the current user.
    Tokens are scrubbed on the way out."""
    repo = McpServersRepo(session)
    context = context_for_auth(ctx, request)
    authorized_ids = await service.list_authorized_ids(
        principal_for_auth(ctx),
        Action.VIEW_METADATA,
        ResourceType.MCP_INSTALLATION,
        context,
    )
    rows = await repo.list_authorized(authorized_ids)
    resources = [_mcp_resource(ctx, row["id"]) for row in rows]
    decisions = await batch_resource_decisions(
        service,
        principal=principal_for_auth(ctx),
        resources=resources,
        context=context,
    )
    provenance = ResourceProvenanceBuilder(session)
    return {
        "items": [
            await _scrub_with_access(
                row,
                provenance,
                decisions[resource],
            )
            for row, resource in zip(rows, resources, strict=True)
        ]
    }


@router.get("/{server_id}")
async def get_mcp_server(
    server_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Fetch a single MCP server by id. 404 if missing or soft-deleted."""
    authorized = await _authorize_mcp(
        request=request,
        ctx=ctx,
        service=service,
        server_id=server_id,
        action=Action.VIEW,
    )
    repo = McpServersRepo(session)
    row = await repo.get(server_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="mcp server not found",
        )
    return await _scrub_with_access(
        row,
        ResourceProvenanceBuilder(session),
        authorized.decision,
    )


@router.post("/{server_id}/oauth/start")
async def start_mcp_oauth_connection(
    server_id: uuid.UUID,
    body: OAuthStartBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_mcp(
        request=request,
        ctx=ctx,
        service=service,
        server_id=server_id,
        action=Action.MANAGE_SECRET,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = McpServersRepo(session)
    server = await repo.get(server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mcp server not found")
    if server.get("auth_mode") != "oauth":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this MCP server does not use OAuth")
    request_origin = request.headers.get("origin")
    if request_origin and request_origin.rstrip("/") != body.return_origin.rstrip("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="return_origin does not match the requesting page")
    server = await hydrate_connection_credentials(session, server)
    try:
        authorization_url = await begin_connection(
            session,
            server=server,
            tenant_id=uuid.UUID(ctx.tenant_id),
            user_id=uuid.UUID(ctx.user_id),
            return_origin=body.return_origin,
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        await repo.update(
            server_id, connection_status="connection_failed", enabled=False,
            last_handshake_status=f"OAuth setup failed: {exc}",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    callback_url = config.public_urls.absolute("api/v1/mcp-servers/oauth/callback")
    callback_parts = urlsplit(callback_url)
    return {
        "authorization_url": authorization_url,
        "callback_origin": f"{callback_parts.scheme}://{callback_parts.netloc}",
    }


@router.post("/{server_id}/oauth/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_mcp_oauth_connection(
    server_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_mcp(
        request=request,
        ctx=ctx,
        service=service,
        server_id=server_id,
        action=Action.MANAGE_SECRET,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    server = await McpServersRepo(session).get(server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mcp server not found")
    if server.get("auth_mode") != "oauth":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this MCP server does not use OAuth")
    await disconnect(session, server)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------ patch


@router.patch("/{server_id}")
async def patch_mcp_server(
    server_id: uuid.UUID,
    body: PatchBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Patch an existing MCP server.

    Flow:
      1. Load existing row; 404 if missing.
      2. ``model_dump(exclude_unset=True)`` — note Pydantic v2 already
         converts nested ``AuthConfig`` → dict, so no manual coercion.
      3. If ``endpoint`` OR ``auth_config`` changed → re-handshake using
         the WOULD-BE-NEW values (falling back to existing for whichever
         wasn't patched). ``tool_prefix`` + ``transport`` are immutable
         and reuse the existing row's values.
      4. On a successful re-handshake, re-run the cross-server conflict
         pre-check via ``list_other_tool_names(exclude_id=server_id)``.
         Passing ``exclude_id`` is LOAD-BEARING: without it, the row's
         OWN previous ``last_tool_names`` would be in the comparison set
         and any unchanged tool would falsely 409.
      5. Either way (ok or error), always write the fresh
         ``last_handshake_*`` snapshot — the UI's status badge needs to
         reflect the most recent probe.
      6. ``repo.update`` (no-op if ``fields`` is empty after step 3).
      7. Return scrubbed updated row.
    """
    sensitive_update = any(
        getattr(body, field) is not None
        for field in ("endpoint", "connection_config", "auth_config")
    )
    if {"endpoint", "connection_config", "auth_config"} & body.model_fields_set:
        await require_recent_step_up(ctx)
    authorized = await _authorize_mcp(
        request=request,
        ctx=ctx,
        service=service,
        server_id=server_id,
        action=(
            Action.MANAGE_SECRET if sensitive_update else Action.UPDATE
        ),
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = McpServersRepo(session)
    existing = await repo.get(server_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="mcp server not found",
        )
    hydrated_existing = await hydrate_connection_credentials(session, existing)

    # Pydantic v2: nested AuthConfig is already a dict after model_dump.
    fields: dict = body.model_dump(exclude_unset=True)
    if (
        fields.get("enabled") is True
        and existing.get("auth_mode") == "oauth"
        and existing.get("connection_status") != "connected"
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="connect an account before enabling this MCP server",
        )
    if "connection_config" in fields:
        fields["connection_config"] = _restore_masked_connection(
            fields["connection_config"],
            hydrated_existing.get("connection_config") or {},
        )
    if "endpoint" in fields:
        fields["endpoint"] = _restore_masked_url(
            fields["endpoint"], hydrated_existing["endpoint"]
        )
    if "description" in fields:
        fields["description"] = (fields.get("description") or "").strip()
        fields.setdefault("description_source", "user_edited")
    if fields.get("description_source") == "ai_generated":
        fields["description_generated_at"] = datetime.now(timezone.utc)
    # Detect a credential change BEFORE ``fields`` is augmented with the
    # ``last_handshake_*`` snapshot keys. ``exclude_unset`` means
    # ``auth_config`` is present here ONLY when the client explicitly sent it.
    auth_config_changed = "auth_config" in fields
    connection_material_changed = any(
        key in fields for key in ("endpoint", "connection_config")
    )
    previous_auth_secret_ref = existing.get("auth_secret_ref")
    previous_connection_secret_ref = existing.get("connection_secret_ref")
    resolved_existing_auth = await resolve_oauth_auth_config(session, existing)

    # Re-handshake when the probe-relevant inputs change. Transport and
    # tool_prefix are immutable so we reuse existing values for those.
    if "endpoint" in fields or "auth_config" in fields or "connection_config" in fields:
        new_endpoint = fields.get("endpoint", hydrated_existing["endpoint"])
        new_auth = fields.get(
            "auth_config",
            resolved_existing_auth or {"type": "none"},
        )
        new_connection = fields.get(
            "connection_config",
            hydrated_existing.get("connection_config") or {},
        )
        result = await handshake_one(
            prefix=existing["tool_prefix"],
            transport=existing["transport"],
            endpoint=new_endpoint,
            auth_config=new_auth,
            connection_config=new_connection,
            timeout_s=config.mcp.handshake_timeout_s,
            tenant_id=ctx.tenant_id,
        )
        _reject_unsafe_probe(result)
        status_str = result["status"]
        tool_count = result.get("tool_count")
        tool_names = result.get("tool_names")

        # Only re-run the pre-check on a successful probe (no names to
        # compare on an error). Per-server tool cap also re-applies — a
        # PATCH must not let an over-cap server slip past.
        if status_str == "ok":
            if (
                tool_count is not None
                and tool_count > config.mcp.per_server_tool_cap
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"server exports {tool_count} tools; per-server "
                        f"cap is {config.mcp.per_server_tool_cap}"
                    ),
                )
            new_prefixed = {
                f"{existing['tool_prefix']}__{tn['name']}"
                for tn in (tool_names or [])
            }
            builtin = _builtin_tool_names()
            builtin_overlap = new_prefixed & builtin
            if builtin_overlap:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "tool name conflicts with built-in: "
                        f"{sorted(builtin_overlap)}"
                    ),
                )
            # exclude_id MUST be the row we're patching — otherwise our
            # own previous tool_names would be in the comparison set and
            # any tool we kept across the re-handshake would falsely 409.
            other_names = await repo.list_other_tool_names(
                exclude_id=server_id,
            )
            other_overlap = new_prefixed & other_names
            if other_overlap:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "tool name conflicts with another MCP server: "
                        f"{sorted(other_overlap)}"
                    ),
                )

        # Always write the latest handshake snapshot — even on error.
        # The UI's status badge depends on this being current.
        fields["last_handshake_status"] = status_str
        fields["last_tool_count"] = tool_count
        fields["last_tool_names"] = tool_names
        fields["last_handshake_at"] = datetime.now(timezone.utc)

    if auth_config_changed:
        request_auth = fields["auth_config"]
        next_version = int(existing.get("auth_secret_version") or 0) + 1
        next_ref = None
        if request_auth.get("type") == "bearer":
            next_ref = await secret_service().put_text(
                session,
                tenant_id=ctx.tenant_id,
                purpose="mcp_bearer_token",
                resource_type="mcp_installation",
                resource_id=server_id,
                plaintext=str(request_auth["token"]),
                version=next_version,
            )
        fields["auth_config"] = {
            "type": request_auth.get("type", "none")
        }
        fields["auth_secret_ref"] = next_ref
        fields["auth_secret_version"] = next_version

    if connection_material_changed:
        next_connection_version = int(
            existing.get("connection_secret_version") or 0
        ) + 1
        full_endpoint = fields.get("endpoint", hydrated_existing["endpoint"])
        full_connection = fields.get(
            "connection_config",
            hydrated_existing.get("connection_config") or {},
        )
        (
            fields["endpoint"],
            fields["connection_config"],
            fields["connection_secret_ref"],
        ) = await store_connection_credentials(
            session,
            tenant_id=ctx.tenant_id,
            server_id=server_id,
            endpoint=full_endpoint,
            connection_config=full_connection,
            version=next_connection_version,
        )
        fields["connection_secret_version"] = next_connection_version

    if fields:
        await repo.update(server_id, **fields)
        if auth_config_changed and previous_auth_secret_ref:
            await secret_service().destroy(
                session,
                secret_ref=previous_auth_secret_ref,
                tenant_id=ctx.tenant_id,
            )
        if connection_material_changed and previous_connection_secret_ref:
            await secret_service().destroy(
                session,
                secret_ref=previous_connection_secret_ref,
                tenant_id=ctx.tenant_id,
            )

    # Audit: a credential change fires ONLY when auth_config was patched.
    # NEVER include the auth_config value (the bearer token) in meta.
    if auth_config_changed:
        await record_audit(
            session,
            action=actions.MCP_SERVER_CREDENTIAL_CHANGE,
            actor_user_id=ctx.user_id,
            actor_email=ctx.email,
            target_type=actions.TARGET_MCP_SERVER,
            target_id=str(server_id),
            target_name=existing.get("name") if isinstance(existing, dict)
            else getattr(existing, "name", None),
            outcome="success",
            audit_ctx=extract_request_audit_context(request) if request else None,
            meta={},
        )

    row = await repo.get(server_id)
    return await _scrub_with_access(
        row,
        ResourceProvenanceBuilder(session),
        authorized.decision,
    )


# ----------------------------------------------------------------------- delete


@router.delete(
    "/{server_id}", status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_mcp_server(
    server_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Soft-delete an MCP server. The repo flips ``enabled=FALSE`` AND
    sets ``deleted_at=now()`` in the same UPDATE so the loader's
    enabled-only scan stops yielding it immediately. Idempotent —
    deleting an already-deleted row 404s (the WHERE filters it from the
    pre-load lookup)."""
    await _authorize_mcp(
        request=request,
        ctx=ctx,
        service=service,
        server_id=server_id,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = McpServersRepo(session)
    existing = await repo.get(server_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="mcp server not found",
        )
    # Capture the name BEFORE the soft-delete for the audit snapshot.
    name = existing.get("name") if isinstance(existing, dict) \
        else getattr(existing, "name", None)
    await delete_oauth_transactions_for_server(session, server_id)
    if existing.get("auth_mode") == "oauth":
        # Remove the remote grant when the provider exposes revocation, then
        # delete the encrypted local token. Revocation is best-effort so an
        # unavailable provider cannot prevent the user from uninstalling.
        await disconnect(session, existing)
    else:
        await delete_oauth_connection(session, server_id)
    await repo.soft_delete(server_id)
    if existing.get("auth_secret_ref"):
        await secret_service().destroy(
            session,
            secret_ref=existing["auth_secret_ref"],
            tenant_id=ctx.tenant_id,
        )
    if existing.get("connection_secret_ref"):
        await secret_service().destroy(
            session,
            secret_ref=existing["connection_secret_ref"],
            tenant_id=ctx.tenant_id,
        )
    await record_audit(
        session,
        action=actions.MCP_SERVER_DELETE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=actions.TARGET_MCP_SERVER,
        target_id=str(server_id),
        target_name=name,
        outcome="success",
        audit_ctx=extract_request_audit_context(request) if request else None,
        meta={},
    )
    if request is not None:
        coordinator = mutation_coordinator_for_request(
            request,
            ctx.active_organization_id,
        )
        mutation_ids = await enqueue_structural_delta(
            session=session,
            coordinator=coordinator,
            actor_type="user",
            actor_id=ctx.user_id,
            before=resource_root_edges(
                organization_id=ctx.active_organization_id,
                object_type="mcp_installation",
                object_id=str(server_id),
                owner_relation="installer",
                owner_type="user",
                owner_id=str(existing["user_id"]),
            ),
            after=frozenset(),
            operation_id=f"mcp-installation:{server_id}:delete",
            source="mcp-installation-delete",
        )
        await session.commit()
        await apply_committed_structural_mutations(
            coordinator,
            mutation_ids,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------- refresh


@router.post("/{server_id}/refresh")
async def refresh_mcp_server(
    server_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Manual re-handshake without changing any config. Reuses the
    existing endpoint / auth / transport / prefix and writes back the
    latest ``last_handshake_*`` snapshot. The frontend "Refresh"
    button on the Settings tab calls this.

    Unlike PATCH, this does NOT re-run the cross-server conflict
    pre-check — the prefix and other immutable identifiers are
    unchanged, so any conflict here means we're already in a bad
    state that the loader would handle (idempotent re-discovery on
    each run)."""
    authorized = await _authorize_mcp(
        request=request,
        ctx=ctx,
        service=service,
        server_id=server_id,
        action=Action.MANAGE_SECRET,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = McpServersRepo(session)
    existing = await repo.get(server_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="mcp server not found",
        )
    hydrated_existing = await hydrate_connection_credentials(session, existing)
    auth_config = await resolve_oauth_auth_config(session, existing)
    if existing.get("auth_mode") == "oauth" and auth_config is None:
        row = await repo.get(server_id)
        return await _scrub_with_access(
            row,
            ResourceProvenanceBuilder(session),
            authorized.decision,
        )
    result = await handshake_one(
        prefix=existing["tool_prefix"],
        transport=existing["transport"],
        endpoint=hydrated_existing["endpoint"],
        auth_config=auth_config or existing["auth_config"],
        connection_config=hydrated_existing.get("connection_config") or {},
        timeout_s=config.mcp.handshake_timeout_s,
        tenant_id=ctx.tenant_id,
    )
    _reject_unsafe_probe(result)
    await repo.update(
        server_id,
        connection_status=(
            "connected" if existing.get("auth_mode") == "oauth" and result["status"] == "ok"
            else existing.get("connection_status", "not_required")
        ),
        enabled=(
            result["status"] == "ok" if existing.get("auth_mode") == "oauth"
            else existing["enabled"]
        ),
        last_handshake_at=datetime.now(timezone.utc),
        last_handshake_status=result["status"],
        last_tool_count=result.get("tool_count"),
        last_tool_names=result.get("tool_names"),
    )
    row = await repo.get(server_id)
    return await _scrub_with_access(
        row,
        ResourceProvenanceBuilder(session),
        authorized.decision,
    )
