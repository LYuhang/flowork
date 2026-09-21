"""API Management Center routes for LLM credentials.

A private, tenant-scoped store of the user's own "bring your own key" LLM
configs. Centralizes what PromptNode today stores INLINE + plaintext in
``node_config.custom_model_config``. PromptNode / agent integration is a LATER
phase — this file only ships the store + its data layer.

THE SECRECY CONTRACT (mirrors how ``routes/mcp_servers.py`` scrubs its bearer
token) is enforced by three out-shapes:

  * ``GET ""``            → ``list[CredentialPublicOut]`` (id/name/description/
    provider only — the surface the PromptNode/agent picker will consume).
  * ``GET /{id}``         → ``CredentialOwnerOut`` (owner edit view: adds
    model_name/api_url + ``api_key_set`` flag; NO plaintext key).
  * ``POST ""`` create / ``PUT /{id}`` update → ``CredentialOwnerOut`` (the
    plaintext key is never echoed back).
  * ``DELETE /{id}``      → 204, soft delete.

Route ordering: ``/{id}/reveal`` is a sub-path of ``/{id}`` so FastAPI matches
it fine regardless of order, but list (``""``) is registered before the
``/{id}`` catch-alls per the MCP file's convention.

Audit: reversible key material is created, rotated and destroyed only through
``SecretService``, which records ``secret.create`` / ``secret.destroy`` in the
same transaction. There is no plaintext reveal route. Metadata authorization
mutations are separately captured by the durable OpenFGA mutation ledger.

Trust boundary (G4b): ``CredentialCreate`` / ``CredentialUpdate`` are
``extra='forbid'`` so a client cannot smuggle tenant_id / user_id / id /
enabled. Identity columns come exclusively from ``AuthContext``.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
from vibecanvas_api.schemas.access import access_from_decision
from vibecanvas_api.schemas.llm_credentials import (
    CredentialCreate,
    CredentialConnectionTestOut,
    CredentialOwnerOut,
    CredentialPublicOut,
    CredentialUpdate,
    OpenRouterCallbackIn,
    OpenRouterConnectionOut,
    OpenRouterStartOut,
)
from vibecanvas_api.audit import actions as audit_actions
from vibecanvas_api.audit.context import extract_request_audit_context
from vibecanvas_api.audit.service import record_audit, record_detached_audit
from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.services.llm_connection_secrets import (
    hydrate_llm_connection_credentials,
    store_llm_connection_credentials,
)
from vibecanvas_api.services.pinned_http import PinnedAsyncHTTPTransport
from vibecanvas_api.services.agent_runtime.registry import AVAILABLE_RUNTIME_TYPES
from vibecanvas_api.services.openrouter_connection import (
    CATALOG_TTL_SECONDS,
    OPENROUTER_API_BASE_URL,
    PKCE_TTL_SECONDS,
    OpenRouterConnectionError,
    authorization_url as openrouter_authorization_url,
    callback_url as openrouter_callback_url,
    exchange_authorization_code,
    fetch_user_model_catalog,
    merge_catalog_with_current,
    new_pkce_material,
    state_digest,
)
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo
from vibecanvas_api.routes.runtime_model_broker import (
    _default_base_url,
    _normalized_provider,
    _validated_user_destination,
)

router = APIRouter(prefix="/api/v1/llm-credentials", tags=["llm-credentials"])


# --------------------------------------------------------------- serializers


def _public_out(
    row: dict,
    decision: Decision | None = None,
) -> CredentialPublicOut:
    """List / public projection. NEVER touches model_name / api_url /
    api_key."""
    return CredentialPublicOut(
        id=str(row["id"]),
        name=row["name"],
        description=row.get("description"),
        provider=row["provider"],
        connection_kind=row.get("connection_kind") or "manual",
        runtime_scope=row.get("runtime_scope") or "codex",
        model_context_tokens=row.get("model_context_tokens"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        access=access_from_decision(decision) if decision else None,
    )


def _owner_out(
    row: dict,
    decision: Decision | None = None,
) -> CredentialOwnerOut:
    """Owner edit projection: non-secret config + ``api_key_set`` flag.
    The plaintext key is NEVER serialized here."""
    return CredentialOwnerOut(
        id=str(row["id"]),
        name=row["name"],
        description=row.get("description"),
        provider=row["provider"],
        connection_kind=row.get("connection_kind") or "manual",
        runtime_scope=row.get("runtime_scope") or "codex",
        model_name=row["model_name"],
        model_context_tokens=row.get("model_context_tokens"),
        api_url=row.get("api_url"),
        proxy=row.get("proxy"),
        api_key_set=bool(row.get("secret_ref")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        access=access_from_decision(decision) if decision else None,
    )


def _credential_resource(
    ctx: AuthContext,
    credential_id: uuid.UUID | str,
) -> ResourceRef:
    return ResourceRef(
        ResourceType.LLM_CREDENTIAL,
        str(credential_id),
        ctx.active_organization_id,
    )


async def _authorize_credential(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    credential_id: uuid.UUID | str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = _credential_resource(ctx, credential_id)
    decision = await service.check(
        principal_for_auth(ctx),
        action,
        resource,
        context_for_auth(ctx, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="credential not found",
        )
    return AuthorizedResource(resource=resource, decision=decision)


async def _rebind_request_organization(
    session: AsyncSession,
    ctx: AuthContext,
) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :organization_id, true)"),
        {"organization_id": ctx.active_organization_id},
    )


def _openrouter_out(row: dict | None) -> OpenRouterConnectionOut:
    if row is None:
        return OpenRouterConnectionOut(connected=False)
    refreshed_at = row.get("catalog_refreshed_at")
    stale = (
        refreshed_at is None
        or refreshed_at < datetime.now(timezone.utc) - timedelta(
            seconds=CATALOG_TTL_SECONDS,
        )
    )
    error_code = row.get("catalog_error_code")
    return OpenRouterConnectionOut(
        connected=error_code != "openrouter_credentials_rejected",
        credential_id=str(row["id"]),
        models=row.get("model_catalog") or [],
        catalog_refreshed_at=refreshed_at,
        catalog_stale=stale,
        error_code=error_code,
    )


async def _openrouter_api_key(session: AsyncSession, row: dict) -> str:
    return await secret_service().resolve_text(
        session,
        secret_ref=row["secret_ref"],
        tenant_id=row["tenant_id"],
        purpose="llm_api_key",
        resource_type="llm_credential",
        resource_id=row["id"],
    )


async def _record_openrouter_failure(
    *, request: Request, ctx: AuthContext, operation: str, code: str,
) -> None:
    await record_detached_audit(
        action=audit_actions.LLM_CREDENTIAL_CONNECTION_CHANGE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        tenant_id=ctx.active_organization_id,
        target_type=audit_actions.TARGET_LLM_CREDENTIAL,
        outcome="failure",
        audit_ctx=extract_request_audit_context(request),
        meta={"provider": "openrouter", "operation": operation, "code": code},
    )


async def _cleanup_expired_openrouter_states(
    session: AsyncSession, ctx: AuthContext,
) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT id, verifier_secret_ref FROM openrouter_oauth_states "
                "WHERE user_id=:user_id AND expires_at <= now() "
                "FOR UPDATE SKIP LOCKED LIMIT 50"
            ),
            {"user_id": uuid.UUID(ctx.user_id)},
        )
    ).mappings().all()
    for row in rows:
        await secret_service().destroy(
            session,
            secret_ref=row["verifier_secret_ref"],
            tenant_id=ctx.active_organization_id,
        )
        await session.execute(
            text("DELETE FROM openrouter_oauth_states WHERE id=:id"),
            {"id": row["id"]},
        )


@router.get("/openrouter/status", response_model=OpenRouterConnectionOut)
async def get_openrouter_status(
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
) -> OpenRouterConnectionOut:
    row = await LlmCredentialsRepo(session).get_openrouter_for_user(ctx.user_id)
    return _openrouter_out(row)


@router.post("/openrouter/start", response_model=OpenRouterStartOut)
async def start_openrouter_connection(
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> OpenRouterStartOut:
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
    try:
        state, verifier, challenge = new_pkce_material()
        callback = openrouter_callback_url(state)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "openrouter_public_url_required"},
        ) from exc
    await _cleanup_expired_openrouter_states(session, ctx)
    state_id = uuid.uuid4()
    verifier_ref = await secret_service().put_text(
        session,
        tenant_id=ctx.active_organization_id,
        purpose="openrouter_pkce_verifier",
        resource_type="openrouter_oauth_state",
        resource_id=state_id,
        plaintext=verifier,
    )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=PKCE_TTL_SECONDS)
    await session.execute(
        text(
            "INSERT INTO openrouter_oauth_states "
            "(id, tenant_id, user_id, state_hash, verifier_secret_ref, expires_at) "
            "VALUES (:id, :tenant_id, :user_id, :state_hash, :secret_ref, :expires_at)"
        ),
        {
            "id": state_id,
            "tenant_id": uuid.UUID(ctx.active_organization_id),
            "user_id": uuid.UUID(ctx.user_id),
            "state_hash": state_digest(state),
            "secret_ref": verifier_ref,
            "expires_at": expires_at,
        },
    )
    await record_audit(
        session,
        action=audit_actions.LLM_CREDENTIAL_CONNECTION_CHANGE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=audit_actions.TARGET_LLM_CREDENTIAL,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"provider": "openrouter", "operation": "start"},
    )
    return OpenRouterStartOut(
        authorization_url=openrouter_authorization_url(
            callback=callback, challenge=challenge,
        ),
        expires_at=expires_at,
    )


@router.post("/openrouter/callback", response_model=OpenRouterConnectionOut)
async def complete_openrouter_connection(
    body: OpenRouterCallbackIn,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> OpenRouterConnectionOut:
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
    state_row = (
        await session.execute(
            text(
                "SELECT id, verifier_secret_ref FROM openrouter_oauth_states "
                "WHERE state_hash=:state_hash AND user_id=:user_id "
                "AND consumed_at IS NULL AND expires_at > now() FOR UPDATE"
            ),
            {"state_hash": state_digest(body.state), "user_id": uuid.UUID(ctx.user_id)},
        )
    ).mappings().one_or_none()
    if state_row is None:
        await _record_openrouter_failure(
            request=request, ctx=ctx, operation="callback",
            code="openrouter_state_invalid",
        )
        raise HTTPException(400, detail={"code": "openrouter_state_invalid"})

    state_id = state_row["id"]
    verifier = await secret_service().resolve_text(
        session,
        secret_ref=state_row["verifier_secret_ref"],
        tenant_id=ctx.active_organization_id,
        purpose="openrouter_pkce_verifier",
        resource_type="openrouter_oauth_state",
        resource_id=state_id,
    )
    await secret_service().destroy(
        session,
        secret_ref=state_row["verifier_secret_ref"],
        tenant_id=ctx.active_organization_id,
    )
    await session.execute(
        text("DELETE FROM openrouter_oauth_states WHERE id=:id"),
        {"id": state_id},
    )
    # Commit the one-time claim before contacting the provider. A failed or
    # replayed exchange can never resurrect the state through rollback.
    await session.commit()
    await _rebind_request_organization(session, ctx)

    try:
        api_key = await exchange_authorization_code(code=body.code, verifier=verifier)
    except OpenRouterConnectionError as exc:
        await _record_openrouter_failure(
            request=request, ctx=ctx, operation="callback", code=exc.code,
        )
        raise HTTPException(502, detail={"code": exc.code}) from exc

    catalog_error: str | None = None
    try:
        fresh_models = await fetch_user_model_catalog(api_key)
    except OpenRouterConnectionError as exc:
        fresh_models = None
        catalog_error = exc.code

    # Serialize concurrent successful callbacks for this user before the
    # partial-unique connection row is read or created.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"openrouter:{ctx.active_organization_id}:{ctx.user_id}"},
    )
    repo = LlmCredentialsRepo(session)
    existing = await repo.get_openrouter_for_user(ctx.user_id)
    models = (
        fresh_models
        if fresh_models is not None
        else list(existing.get("model_catalog") or []) if existing else []
    )

    credential_id = uuid.UUID(str(existing["id"])) if existing else uuid.uuid4()
    secret_ref = await secret_service().put_text(
        session,
        tenant_id=ctx.active_organization_id,
        purpose="llm_api_key",
        resource_type="llm_credential",
        resource_id=credential_id,
        plaintext=api_key,
        version=int(existing.get("secret_version") or 0) + 1 if existing else 1,
    )
    selected_model = (
        str(existing.get("model_name") or "") if existing else ""
    )
    if not selected_model and models:
        selected_model = models[0]["id"]
    selected_model = selected_model or "openrouter/auto"
    models = merge_catalog_with_current(models, current_model_id=selected_model)
    model_context = next(
        (
            item.get("context_length") for item in models
            if item["id"] == selected_model
        ),
        None,
    )
    now = datetime.now(timezone.utc)
    mutation_ids: list[str] = []
    if existing:
        previous_secret_ref = existing.get("secret_ref")
        await repo.update(
            credential_id,
            provider="openrouter",
            runtime_scope="codex",
            connection_kind="openrouter_oauth",
            model_name=selected_model,
            model_context_tokens=model_context,
            api_url=OPENROUTER_API_BASE_URL,
            secret_ref=secret_ref,
            secret_version=int(existing.get("secret_version") or 0) + 1,
            model_catalog=json.dumps(models),
            catalog_refreshed_at=(
                now if not catalog_error else existing.get("catalog_refreshed_at")
            ),
            catalog_error_code=catalog_error,
            enabled=True,
        )
        if previous_secret_ref:
            await secret_service().destroy(
                session,
                secret_ref=previous_secret_ref,
                tenant_id=ctx.active_organization_id,
            )
        operation = "reconnect"
    else:
        await repo.insert(
            id=credential_id,
            tenant_id=uuid.UUID(ctx.active_organization_id),
            user_id=uuid.UUID(ctx.user_id),
            name=f"OpenRouter · {str(credential_id)[:8]}",
            description="Connected OpenRouter account",
            provider="openrouter",
            runtime_scope="codex",
            connection_kind="openrouter_oauth",
            model_name=selected_model,
            model_context_tokens=model_context,
            model_catalog=json.dumps(models),
            catalog_refreshed_at=now if not catalog_error else None,
            catalog_error_code=catalog_error,
            api_url=OPENROUTER_API_BASE_URL,
            secret_ref=secret_ref,
            secret_version=1,
            enabled=True,
        )
        coordinator = mutation_coordinator_for_request(
            request, ctx.active_organization_id,
        )
        mutation_ids = await enqueue_structural_delta(
            session=session,
            coordinator=coordinator,
            actor_type="user",
            actor_id=ctx.user_id,
            before=frozenset(),
            after=resource_root_edges(
                organization_id=ctx.active_organization_id,
                object_type="llm_credential",
                object_id=str(credential_id),
                owner_relation="owner",
                owner_type="user",
                owner_id=ctx.user_id,
            ),
            operation_id=f"llm-credential:{credential_id}:openrouter-connect",
            source="openrouter-connect",
        )
        operation = "connect"
    await record_audit(
        session,
        action=audit_actions.LLM_CREDENTIAL_CONNECTION_CHANGE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=audit_actions.TARGET_LLM_CREDENTIAL,
        target_id=str(credential_id),
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={
            "provider": "openrouter", "operation": operation,
            "catalog_models": len(models), "catalog_error": catalog_error,
        },
    )
    await session.commit()
    if mutation_ids:
        await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_request_organization(session, ctx)
    return _openrouter_out(await repo.get_openrouter_for_user(ctx.user_id))


@router.post("/openrouter/refresh", response_model=OpenRouterConnectionOut)
async def refresh_openrouter_catalog(
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> OpenRouterConnectionOut:
    repo = LlmCredentialsRepo(session)
    row = await repo.get_openrouter_for_user(ctx.user_id)
    if row is None:
        raise HTTPException(404, detail={"code": "openrouter_not_connected"})
    await _authorize_credential(
        request=request, ctx=ctx, service=service,
        credential_id=row["id"], action=Action.MANAGE_SECRET,
    )
    try:
        models = await fetch_user_model_catalog(
            await _openrouter_api_key(session, row),
        )
    except OpenRouterConnectionError as exc:
        await repo.update(row["id"], catalog_error_code=exc.code)
        await record_audit(
            session,
            action=audit_actions.LLM_CREDENTIAL_CONNECTION_CHANGE,
            actor_user_id=ctx.user_id,
            actor_email=ctx.email,
            target_type=audit_actions.TARGET_LLM_CREDENTIAL,
            target_id=str(row["id"]),
            outcome="failure",
            audit_ctx=extract_request_audit_context(request),
            meta={"provider": "openrouter", "operation": "refresh", "code": exc.code},
        )
        updated = await repo.get_openrouter_for_user(ctx.user_id)
        return _openrouter_out(updated)
    models = merge_catalog_with_current(
        models, current_model_id=str(row.get("model_name") or ""),
    )
    await repo.update(
        row["id"],
        model_catalog=json.dumps(models),
        catalog_refreshed_at=datetime.now(timezone.utc),
        catalog_error_code=None,
    )
    await record_audit(
        session,
        action=audit_actions.LLM_CREDENTIAL_CONNECTION_CHANGE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=audit_actions.TARGET_LLM_CREDENTIAL,
        target_id=str(row["id"]),
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"provider": "openrouter", "operation": "refresh", "catalog_models": len(models)},
    )
    return _openrouter_out(await repo.get_openrouter_for_user(ctx.user_id))


@router.delete("/openrouter", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_openrouter(
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> Response:
    repo = LlmCredentialsRepo(session)
    row = await repo.get_openrouter_for_user(ctx.user_id)
    if row is None:
        raise HTTPException(404, detail={"code": "openrouter_not_connected"})
    credential_id = uuid.UUID(str(row["id"]))
    await _authorize_credential(
        request=request, ctx=ctx, service=service,
        credential_id=credential_id, action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    await repo.soft_delete(credential_id)
    if row.get("secret_ref"):
        await secret_service().destroy(
            session,
            secret_ref=row["secret_ref"],
            tenant_id=ctx.active_organization_id,
        )
    coordinator = mutation_coordinator_for_request(request, ctx.active_organization_id)
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=ctx.user_id,
        before=resource_root_edges(
            organization_id=ctx.active_organization_id,
            object_type="llm_credential",
            object_id=str(credential_id),
            owner_relation="owner",
            owner_type="user",
            owner_id=ctx.user_id,
        ),
        after=frozenset(),
        operation_id=f"llm-credential:{credential_id}:openrouter-disconnect",
        source="openrouter-disconnect",
    )
    await record_audit(
        session,
        action=audit_actions.LLM_CREDENTIAL_CONNECTION_CHANGE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=audit_actions.TARGET_LLM_CREDENTIAL,
        target_id=str(credential_id),
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"provider": "openrouter", "operation": "disconnect"},
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------- create


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_credential(
    body: CredentialCreate,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> CredentialOwnerOut:
    """Create a credential for the current tenant. Returns the owner view —
    the plaintext key is NOT echoed back. The partial UNIQUE index on
    ``(tenant_id, name) WHERE deleted_at IS NULL`` surfaces as an
    IntegrityError → 409."""
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
    if body.runtime_scope not in AVAILABLE_RUNTIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="unsupported runtime scope",
        )
    repo = LlmCredentialsRepo(session)
    cid = uuid.uuid4()
    secret_ref = await secret_service().put_text(
        session,
        tenant_id=ctx.active_organization_id,
        purpose="llm_api_key",
        resource_type="llm_credential",
        resource_id=cid,
        plaintext=body.api_key,
    )
    stored_api_url, stored_proxy, connection_secret_ref = (
        await store_llm_connection_credentials(
            session,
            tenant_id=ctx.active_organization_id,
            credential_id=cid,
            api_url=body.api_url,
            proxy=body.proxy,
            version=1,
        )
    )
    fields = dict(
        id=cid,
        tenant_id=uuid.UUID(ctx.tenant_id),
        user_id=uuid.UUID(ctx.user_id),
        name=body.name,
        description=body.description,
        provider=body.provider,
        runtime_scope=body.runtime_scope,
        model_name=body.model_name,
        model_context_tokens=body.model_context_tokens,
        api_url=stored_api_url,
        proxy=stored_proxy,
        connection_secret_ref=connection_secret_ref,
        connection_secret_version=1,
        secret_ref=secret_ref,
        secret_version=1,
        enabled=True,
    )
    try:
        await repo.insert(**fields)
        await session.flush()  # surface IntegrityError NOW, not at request end
    except IntegrityError as e:
        # Only the live-name partial UNIQUE index is a user conflict.  Do not
        # disguise FK/CHECK/NOT NULL failures as "already exists": those are
        # storage/security-cutover defects that operators must be able to see.
        if getattr(e.orig, "sqlstate", None) != "23505":
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a credential with this name already exists",
        ) from e
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
            object_type="llm_credential",
            object_id=str(cid),
            owner_relation="owner",
            owner_type="user",
            owner_id=ctx.user_id,
        ),
        operation_id=f"llm-credential:{cid}:create",
        source="llm-credential-create",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_request_organization(session, ctx)
    row = await repo.get(cid)
    authorized = await _authorize_credential(
        request=request,
        ctx=ctx,
        service=service,
        credential_id=cid,
        action=Action.MANAGE_SECRET,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    return _owner_out(row, authorized.decision)


# ----------------------------------------------------------------------- list


@router.get("")
async def list_credentials(
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> list[CredentialPublicOut]:
    """List every live credential for the current tenant. PUBLIC projection
    only (id/name/description/provider) — this is the surface PromptNode /
    agent pickers consume; it MUST NOT leak model_name / api_url / api_key."""
    repo = LlmCredentialsRepo(session)
    context = context_for_auth(ctx, request)
    authorized_ids = await service.list_authorized_ids(
        principal_for_auth(ctx),
        Action.VIEW_METADATA,
        ResourceType.LLM_CREDENTIAL,
        context,
    )
    rows = await repo.list_authorized(authorized_ids)
    resources = [
        _credential_resource(ctx, row["id"]) for row in rows
    ]
    decisions = await batch_resource_decisions(
        service,
        principal=principal_for_auth(ctx),
        resources=resources,
        context=context,
    )
    return [
        _public_out(row, decisions[resource])
        for row, resource in zip(rows, resources, strict=True)
    ]


# ----------------------------------------------------------------------- get


@router.get("/{credential_id}")
async def get_credential(
    credential_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> CredentialOwnerOut:
    """Owner edit view of one credential. 404 if missing / soft-deleted /
    cross-tenant (RLS hides the last equivalently to non-existence). Returns
    model_name + api_url but NO plaintext key."""
    authorized = await _authorize_credential(
        request=request,
        ctx=ctx,
        service=service,
        credential_id=credential_id,
        action=Action.MANAGE_SECRET,
    )
    repo = LlmCredentialsRepo(session)
    row = await repo.get(credential_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="credential not found",
        )
    return _owner_out(row, authorized.decision)


@router.post("/{credential_id}/test")
async def test_credential_connection(
    credential_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> CredentialConnectionTestOut:
    """Probe a provider using the stored write-only secret.

    The request follows the same validated egress destination and proxy rules
    as normal model traffic. Only a bounded status classification and latency
    are returned; provider bodies, headers, and credentials are discarded.
    """
    await _authorize_credential(
        request=request,
        ctx=ctx,
        service=service,
        credential_id=credential_id,
        action=Action.MANAGE_SECRET,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    row = await LlmCredentialsRepo(session).get(credential_id)
    if not row or not row.get("enabled") or not row.get("secret_ref"):
        raise HTTPException(status_code=404, detail="credential not found")

    hydrated = await hydrate_llm_connection_credentials(session, row)
    provider = _normalized_provider(row.get("provider"))
    base_url = str(hydrated.get("api_url") or _default_base_url(provider))
    if not base_url:
        raise HTTPException(
            status_code=409,
            detail={"code": "credential_destination_missing"},
        )
    api_key = await secret_service().resolve_text(
        session,
        secret_ref=row["secret_ref"],
        tenant_id=row["tenant_id"],
        purpose="llm_api_key",
        resource_type="llm_credential",
        resource_id=row["id"],
    )

    base_url, host, addresses = await _validated_user_destination(
        base_url,
        label="model API URL",
    )
    pinned: dict[str, tuple[str, ...]] = {}
    if addresses:
        pinned[host] = addresses
    proxy = str(hydrated.get("proxy") or "") or None
    if proxy:
        proxy, proxy_host, proxy_addresses = await _validated_user_destination(
            proxy,
            label="model proxy URL",
        )
        if proxy_addresses:
            pinned[proxy_host] = proxy_addresses

    suffix = "/v1beta/models?pageSize=1" if provider in {
        "google", "google_genai", "gemini",
    } else "/v1/models" if provider == "anthropic" else "/models"
    target_url = f"{base_url.rstrip('/')}{suffix}"
    headers = {"Accept": "application/json"}
    if provider == "anthropic":
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    elif provider in {"google", "google_genai", "gemini"}:
        headers["x-goog-api-key"] = api_key
    elif provider in {"azure", "azure_openai"}:
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    started = time.monotonic()
    try:
        transport = PinnedAsyncHTTPTransport(addresses=pinned, proxy=proxy)
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(8.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            # Inspect headers/status only. Do not buffer an untrusted provider
            # body merely to prove that the configured endpoint is reachable.
            async with client.stream("GET", target_url, headers=headers) as response:
                upstream_status = response.status_code
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        if 200 <= upstream_status < 300:
            outcome = "connected"
        elif upstream_status in {401, 403}:
            outcome = "credentials_rejected"
        else:
            outcome = "endpoint_rejected"
        return CredentialConnectionTestOut(
            ok=outcome == "connected",
            outcome=outcome,
            latency_ms=latency_ms,
            upstream_status=upstream_status,
        )
    except (httpx.HTTPError, OSError):
        return CredentialConnectionTestOut(
            ok=False,
            outcome="unreachable",
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
        )


# ---------------------------------------------------------------------- update


@router.put("/{credential_id}")
async def update_credential(
    credential_id: uuid.UUID,
    body: CredentialUpdate,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> CredentialOwnerOut:
    """Partial update. ``api_key`` omitted OR empty => keep the existing key
    (we never blank a secret on a metadata-only edit). 404 if missing.
    Returns the owner view (no plaintext key)."""
    sensitive_fields = {
        "api_key", "api_url", "proxy", "provider", "runtime_scope",
    }
    if sensitive_fields & body.model_fields_set:
        await require_recent_step_up(ctx)
    authorized = await _authorize_credential(
        request=request,
        ctx=ctx,
        service=service,
        credential_id=credential_id,
        action=Action.MANAGE_SECRET,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = LlmCredentialsRepo(session)
    existing = await repo.get(credential_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="credential not found",
        )

    fields = body.model_dump(exclude_unset=True)
    if (
        "runtime_scope" in fields
        and fields["runtime_scope"] not in AVAILABLE_RUNTIME_TYPES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="unsupported runtime scope",
        )
    new_api_key = fields.pop("api_key", None)
    previous_secret_ref = existing.get("secret_ref")
    previous_connection_secret_ref = existing.get("connection_secret_ref")
    connection_changed = any(key in fields for key in ("api_url", "proxy"))
    hydrated_existing = await hydrate_llm_connection_credentials(
        session, existing
    )
    if new_api_key:
        next_version = int(existing.get("secret_version") or 0) + 1
        fields.update(
            secret_ref=await secret_service().put_text(
                session,
                tenant_id=ctx.active_organization_id,
                purpose="llm_api_key",
                resource_type="llm_credential",
                resource_id=credential_id,
                plaintext=new_api_key,
                version=next_version,
            ),
            secret_version=next_version,
        )

    if connection_changed:
        full_api_url = fields.get("api_url", hydrated_existing.get("api_url"))
        full_proxy = fields.get("proxy", hydrated_existing.get("proxy"))
        # An edit form may submit the exact redacted value it received. Keep
        # the existing full value rather than rotating the mask into storage.
        if full_api_url == existing.get("api_url"):
            full_api_url = hydrated_existing.get("api_url")
        if full_proxy == existing.get("proxy"):
            full_proxy = hydrated_existing.get("proxy")
        next_connection_version = int(
            existing.get("connection_secret_version") or 0
        ) + 1
        (
            fields["api_url"],
            fields["proxy"],
            fields["connection_secret_ref"],
        ) = await store_llm_connection_credentials(
            session,
            tenant_id=ctx.active_organization_id,
            credential_id=credential_id,
            api_url=full_api_url,
            proxy=full_proxy,
            version=next_connection_version,
        )
        fields["connection_secret_version"] = next_connection_version

    if fields:
        try:
            await repo.update(credential_id, **fields)
            await session.flush()
            if new_api_key and previous_secret_ref:
                await secret_service().destroy(
                    session,
                    secret_ref=previous_secret_ref,
                    tenant_id=ctx.active_organization_id,
                )
            if connection_changed and previous_connection_secret_ref:
                await secret_service().destroy(
                    session,
                    secret_ref=previous_connection_secret_ref,
                    tenant_id=ctx.active_organization_id,
                )
        except IntegrityError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a credential with this name already exists",
            ) from e

    row = await repo.get(credential_id)
    return _owner_out(row, authorized.decision)


# ---------------------------------------------------------------------- delete


@router.delete(
    "/{credential_id}", status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_credential(
    credential_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Soft-delete a credential (sets ``deleted_at`` + ``enabled=FALSE``).
    Idempotent — deleting an already-deleted / cross-tenant row 404s (RLS +
    the WHERE filter hide it from the pre-load lookup)."""
    await _authorize_credential(
        request=request,
        ctx=ctx,
        service=service,
        credential_id=credential_id,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = LlmCredentialsRepo(session)
    existing = await repo.get(credential_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="credential not found",
        )
    await repo.soft_delete(credential_id)
    if existing.get("secret_ref"):
        await secret_service().destroy(
            session,
            secret_ref=existing["secret_ref"],
            tenant_id=ctx.active_organization_id,
        )
    if existing.get("connection_secret_ref"):
        await secret_service().destroy(
            session,
            secret_ref=existing["connection_secret_ref"],
            tenant_id=ctx.active_organization_id,
        )
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
            object_type="llm_credential",
            object_id=str(credential_id),
            owner_relation="owner",
            owner_type="user",
            owner_id=str(existing["user_id"]),
        ),
        after=frozenset(),
        operation_id=f"llm-credential:{credential_id}:delete",
        source="llm-credential-delete",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
