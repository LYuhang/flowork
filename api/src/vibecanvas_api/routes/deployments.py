"""Deployments — CRUD routes. T4 ships ``POST /api/v1/deployments``.

Single create path for the two external trigger types (API / webhook).
The handler:

1. Validates the request body (slug pattern, trigger enum and version_pin).
2. Resolves ``pinned_major`` / ``pinned_sub`` when ``version_pin == 'specific'``
   (defaults to current HEAD if the caller did not supply them — 404 if
   the workflow has no versions yet).
3. Generates one-shot plaintext credentials per trigger type:
   - ``api`` → ``api_key`` (returned once; stored as SHA-256 hash).
   - ``webhook`` → ``hmac_secret`` (returned once; envelope-encrypted by
     host-side SecretService).
4. Inserts the row via ``DeploymentsRepo`` under the tenant-bound DI
   session (RLS applies). ``IntegrityError`` becomes a 409 (slug already
   in use OR wf_id FK violation).

Tenant / user identity comes ONLY from the auth context — Pydantic's
``ConfigDict(extra='ignore')`` drops smuggled ``tenant_id`` / ``user_id``
from the body, and the handler reads ``ctx.tenant_id`` / ``ctx.user_id``
explicitly. This is gate G4b ("trust boundary") for the deployments
table, mirroring the durable batch-submit transaction pattern.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime
from time import perf_counter
from typing import Annotated, List, Literal, Optional
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
    context_for_auth,
    get_authz_service,
    mutation_coordinator_for_request,
    principal_for_auth,
)
from vibecanvas_api.authorization.mutations import AuthzMutationError
from vibecanvas_api.authorization.share_resolution import (
    binding_from_share_resolution,
)
from vibecanvas_api.authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    resource_root_edges,
    service_account_edges,
)
from vibecanvas_api.authorization.service import (
    AuthorizationDeniedError,
    AuthzService,
    batch_resource_decisions,
)
from vibecanvas_api.authorization.types import (
    Action,
    AuthorizedResource,
    ConsistencyPreference,
    Decision,
    RelationshipBinding,
    RelationshipSubject,
    RelationshipSubjectType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.schemas.access import (
    DirectBindingGrantIn,
    DirectBindingIn,
    DirectBindingListOut,
    DirectBindingOut,
    access_from_decision,
)
from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.services.secrets import generate_api_key, generate_hmac_secret
from vibecanvas_api.services.access_presentation import direct_binding_out
from vibecanvas_api.services.resource_provenance import (
    ResourceProvenanceBuilder,
)
from vibecanvas_api.services.service_account_credentials import (
    bind_workflow_credentials,
)
from vibecanvas_api.services.workflow_runner import (
    load_workflow_version,
    run_workflow_sandboxed_sync,
)
from vibecanvas_api.services.deployment_snapshots import resolve_workflow
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo

router = APIRouter(prefix="/api/v1/deployments", tags=["deployments"])


# Module-level constants — keep cheap to import-time validate.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ALLOWED_VERSION_PIN = frozenset({"head", "major", "specific"})


def _deployment_resource(
    ctx: AuthContext,
    deployment_id: uuid.UUID | str,
) -> ResourceRef:
    return ResourceRef(
        ResourceType.DEPLOYMENT,
        str(deployment_id),
        ctx.active_organization_id,
    )


async def _authorize_deployment(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    deployment_id: uuid.UUID | str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = _deployment_resource(ctx, deployment_id)
    decision = await service.check(
        principal_for_auth(ctx),
        action,
        resource,
        context_for_auth(ctx, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="resource_not_found")
    return AuthorizedResource(resource=resource, decision=decision)


async def _authorize_organization_create(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
) -> None:
    decision = await service.check(
        principal_for_auth(ctx),
        Action.CREATE,
        ResourceRef(
            ResourceType.ORGANIZATION,
            ctx.active_organization_id,
            ctx.active_organization_id,
        ),
        context_for_auth(ctx, request),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="resource_not_found")


async def _authorize_workflow_deploy(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    workflow_id: str,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> None:
    decision = await service.check(
        principal_for_auth(ctx),
        Action.DEPLOY,
        ResourceRef(
            ResourceType.WORKFLOW,
            workflow_id,
            ctx.active_organization_id,
        ),
        context_for_auth(ctx, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="resource_not_found")


async def _rebind_request_organization(
    session: AsyncSession,
    ctx: AuthContext,
) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :organization_id, true)"),
        {"organization_id": ctx.active_organization_id},
    )


def _binding_out(binding: RelationshipBinding) -> DirectBindingOut:
    return DirectBindingOut(
        relation=binding.relation,
        subject_type=binding.subject.type.value,
        subject_id=binding.subject.id,
        subject_relation=binding.subject.relation,
    )


def _binding_from_body(
    body: DirectBindingIn | DirectBindingGrantIn,
    *,
    ctx: AuthContext,
    deployment_id: uuid.UUID,
) -> RelationshipBinding:
    return RelationshipBinding(
        subject=RelationshipSubject(
            type=RelationshipSubjectType(body.subject_type),
            id=body.subject_id,
            relation=body.subject_relation,
        ),
        relation=body.relation,
        resource=_deployment_resource(ctx, deployment_id),
    )


def _require_sharing_enabled() -> None:
    if not config.resource_sharing_enabled:
        raise HTTPException(status_code=404, detail="resource_not_found")


class CreateDeploymentBody(BaseModel):
    """Body schema for ``POST /api/v1/deployments``.

    ``ConfigDict(extra='ignore')`` drops every unknown field — a client
    CANNOT smuggle ``tenant_id`` / ``user_id`` / ``api_key_hash`` /
    ``hmac_secret`` / ``id`` into the row. The handler derives those
    from ``AuthContext`` or the secret-generator. This is the trust
    transaction boundary used by invocation authorization.
    """

    model_config = ConfigDict(extra="ignore")

    wf_id: str
    name: str = Field(..., min_length=1, max_length=200)
    slug: str
    trigger_type: Literal["api", "webhook"]
    version_pin: str
    pinned_major: Optional[int] = None
    pinned_sub: Optional[int] = None
    rate_limit_qps: int = 10
    mount_enabled: bool = False
    enabled: bool = True

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9-]{0,62}$ "
                "(lowercase alphanumeric + hyphens, 1-63 chars, "
                "no leading hyphen)"
            )
        return v

    @field_validator("version_pin")
    @classmethod
    def _vp(cls, v: str) -> str:
        if v not in _ALLOWED_VERSION_PIN:
            raise ValueError("version_pin must be one of: head, major, specific")
        return v

    @field_validator("rate_limit_qps")
    @classmethod
    def _qps(cls, v: int) -> int:
        if v < 0:
            raise ValueError("rate_limit_qps must be >= 0")
        return v

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_deployment(
    body: CreateDeploymentBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Create an API or webhook deployment. Returns ``{id, ...one-shot
    plaintext credentials...}``. Subsequent GETs (T5) MUST NOT include
    the plaintext credentials.

    Resolution rules for ``pinned_major`` / ``pinned_sub``:
    * ``version_pin='head'`` → both forced to ``None`` (the row tracks
      the workflow's HEAD pointer; invocation resolves it at run time).
    * ``version_pin='specific'`` + both supplied → trust the caller (FK
      / CHECK constraint will reject an invalid pair at INSERT time).
    * ``version_pin='specific'`` + either missing → default to the
      workflow's current HEAD. 404 if the workflow has no versions yet
      (a workflow with zero rows in ``workflow_versions`` is not deployable).

    409 on IntegrityError: global partial UNIQUE on ``slug WHERE
    deleted_at IS NULL`` (migration 088) OR the ``wf_id`` FK to
    ``workflows`` (a missing / foreign-tenant workflow). Both collapse
    to a single user-facing message so we don't leak existence.
    """
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    await _authorize_workflow_deploy(
        request=request,
        ctx=ctx,
        service=service,
        workflow_id=body.wf_id,
    )
    repo = DeploymentsRepo(session)

    pinned_major = body.pinned_major
    pinned_sub = body.pinned_sub
    if body.version_pin == "head":
        pinned_major = None
        pinned_sub = None
    elif body.version_pin == "major":
        if pinned_major is None or pinned_major < 1:
            raise HTTPException(422, "A positive pinned_major is required for major tracking.")
        pinned_sub = None
    else:  # 'specific'
        if pinned_major is None or pinned_sub is None:
            # Resolve to current HEAD. The session is already
            # tenant-bound, so RLS hides foreign-tenant rows (a foreign
            # wf_id is invisible → 404, same as a missing workflow).
            row = (await session.execute(
                text(
                    "SELECT major, sub FROM workflow_versions "
                    "WHERE wf_id = :w "
                    "ORDER BY major DESC, sub DESC LIMIT 1"
                ),
                {"w": body.wf_id},
            )).one_or_none()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"workflow {body.wf_id} has no versions; cannot "
                        "pin a specific version"
                    ),
                )
            pinned_major = row.major
            pinned_sub = row.sub

    # Resolve before creating identities or secrets. Never fall back from a
    # missing explicit branch/version to another branch.
    selected_graph = await resolve_workflow(session, ctx.user_id, {"wf_id": body.wf_id,
        "version_pin": body.version_pin, "pinned_major": pinned_major, "pinned_sub": pinned_sub})

    # Tenant / user identity ONLY from auth context — never the body.
    dep_id = uuid.uuid4()
    service_account_id = uuid.uuid4()
    await ServiceAccountsRepo(session).create_for_owner(
        service_account_id=service_account_id,
        tenant_id=uuid.UUID(ctx.tenant_id),
        name=f"Deployment: {body.name}",
        kind="deployment",
        owner_resource_type="deployment",
        owner_resource_id=str(dep_id),
        created_by=uuid.UUID(ctx.user_id),
    )
    credential_ids = await bind_workflow_credentials(
        session,
        tenant_id=uuid.UUID(ctx.tenant_id),
        service_account_id=service_account_id,
        created_by=ctx.user_id,
        workflow=selected_graph,
    )
    fields: dict = dict(
        id=dep_id,
        tenant_id=uuid.UUID(ctx.tenant_id),
        user_id=uuid.UUID(ctx.user_id),
        owner_id=uuid.UUID(ctx.user_id),
        service_account_id=service_account_id,
        wf_id=body.wf_id,
        name=body.name,
        slug=body.slug,
        trigger_type=body.trigger_type,
        version_pin=body.version_pin,
        pinned_major=pinned_major,
        pinned_sub=pinned_sub,
        rate_limit_qps=body.rate_limit_qps,
        mount_enabled=body.mount_enabled,
        enabled=body.enabled,
    )
    if not body.enabled:
        await ServiceAccountsRepo(session).set_status(service_account_id, status="disabled")

    # Close the permission-revocation race immediately before generating a
    # one-shot secret and introducing the durable Deployment.
    await _authorize_workflow_deploy(
        request=request,
        ctx=ctx,
        service=service,
        workflow_id=body.wf_id,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    response_extras: dict = {}
    if body.trigger_type == "api":
        plaintext, hashed = generate_api_key()
        fields["api_key_hash"] = hashed
        response_extras["api_key"] = plaintext
        response_extras["endpoint_url"] = (
            f"/api/v1/deployments/{body.slug}/invoke"
        )
    elif body.trigger_type == "webhook":
        secret = generate_hmac_secret()
        fields["hmac_secret_ref"] = await secret_service().put_text(
            session,
            tenant_id=ctx.active_organization_id,
            purpose="deployment_webhook_hmac",
            resource_type="deployment",
            resource_id=dep_id,
            plaintext=secret,
        )
        fields["hmac_secret_version"] = 1
        response_extras["hmac_secret"] = secret
        response_extras["webhook_url"] = (
            f"/api/v1/deployments/{body.slug}/webhook"
        )
    try:
        dep_id = await repo.insert(**fields)
        await session.flush()  # surface IntegrityError NOW, not on dep teardown
    except IntegrityError as e:
        # Two known constraints can fire here, both client-facing:
        # * global partial UNIQUE on slug WHERE deleted_at IS NULL
        #   (migration 088) → public endpoint collision.
        # * FK on wf_id → workflow does not exist (or is foreign-tenant,
        #   RLS-hidden → also surfaces as FK violation since the FK
        #   probe sees no row).
        # Collapse both into a single 409 message — don't leak which
        # one failed.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "create failed: public slug already in use, "
                "or workflow not found"
            ),
        ) from e

    # Audit: never include the one-shot plaintext credential in meta.
    await record_audit(
        session,
        action=actions.DEPLOYMENT_CREATE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=actions.TARGET_DEPLOYMENT,
        target_id=str(dep_id),
        target_name=body.name,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={},
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
        before=frozenset(),
        after=(
            resource_root_edges(
                organization_id=ctx.active_organization_id,
                object_type="deployment",
                object_id=str(dep_id),
                owner_relation="manager",
                owner_type="user",
                owner_id=ctx.user_id,
            )
            | service_account_edges(
                organization_id=ctx.active_organization_id,
                service_account_id=str(service_account_id),
                created_by=ctx.user_id,
                owner_resource_type="deployment",
                owner_resource_id=str(dep_id),
                workflow_id=body.wf_id,
                credential_ids=credential_ids,
            )
        ),
        operation_id=uuid.uuid4().hex,
        source="deployment-create",
    )
    await session.commit()
    try:
        await apply_committed_structural_mutations(coordinator, mutation_ids)
    except Exception:
        # Creation and one-shot credentials already exist; never encourage a
        # duplicate create after an authorization projection outage.
        return {"id": str(dep_id), "authorization_pending": True, **response_extras}
    await _rebind_request_organization(session, ctx)
    decision = await service.check(
        principal_for_auth(ctx),
        Action.VIEW_METADATA,
        _deployment_resource(ctx, dep_id),
        context_for_auth(
            ctx,
            request,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        ),
    )
    if not decision.allowed:
        return {"id": str(dep_id), "authorization_pending": True, **response_extras}
    return {
        "id": str(dep_id),
        "access": access_from_decision(decision).model_dump(mode="json"),
        "provenance": (
            await ResourceProvenanceBuilder(session).build(
                creator_user_id=ctx.user_id,
            )
        ).model_dump(mode="json"),
        **response_extras,
    }


# ---------------------------------------------------------------------------
# Deployments T5 — list / get / patch / delete / rotate-key.
#
# Every read path passes through ``_scrub_secret_fields`` so hashes, legacy
# plaintext and SecretService references NEVER appear in GET responses.
# one-shot returned at create (T4) / rotate-key (this file); from then on
# the caller is responsible for storing it. ``rotate-key`` is the only way
# to obtain a new plaintext for an existing api-type deployment — and only
# the NEW plaintext; the old key is destroyed in the same UPDATE.
#
# Soft-delete (DELETE → ``deleted_at = now()``) is the only delete path.
# Combined with migration 088's global partial UNIQUE on ``slug WHERE
# deleted_at IS NULL``, a deleted deployment frees its public slug for
# reuse but stays auditable.
# ---------------------------------------------------------------------------


class PatchDeploymentBody(BaseModel):
    """Partial update of a deployment. ``extra='ignore'`` so a client
    cannot patch ``tenant_id`` / ``user_id`` / ``api_key_hash`` /
    ``hmac_secret`` / ``wf_id`` / ``trigger_type`` / ``slug`` /
    ``deleted_at`` — those are either immutable or owned by a dedicated
    path (rotate-key for secrets, soft_delete for ``deleted_at``)."""

    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    enabled: Optional[bool] = None
    rate_limit_qps: Optional[int] = None
    version_pin: Optional[str] = None
    pinned_major: Optional[int] = None
    pinned_sub: Optional[int] = None
    mount_enabled: Optional[bool] = None


async def _scrub_secret_fields(
    dep: dict,
    decision: Decision,
    provenance: ResourceProvenanceBuilder,
) -> dict:
    """Strip credential material and references from a deployment row dict
    and normalise UUID / datetime values for JSON serialization.

    Every GET response (single + list) MUST flow through this. After
    T4's one-shot return, the plaintext credential is the caller's
    responsibility — the API never re-exposes the stored material.
    Webhook verification resolves its SecretRef host-side; clients see only
    the one-shot plaintext returned during creation.
    """
    out = dict(dep)
    out.pop("api_key_hash", None)
    out.pop("hmac_secret_ref", None)
    out.pop("hmac_secret_version", None)
    # UUIDs → str (FastAPI's default encoder handles UUID but JSON tests
    # equality-compare against ``str(uuid)``; normalise here so callers
    # don't have to coerce).
    for key in ("id", "tenant_id", "user_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    # datetimes → isoformat strings (asyncpg returns ``datetime`` objects).
    for key in ("created_at", "updated_at", "last_invoked_at", "deleted_at"):
        v = out.get(key)
        if v is not None and hasattr(v, "isoformat"):
            out[key] = v.isoformat()
    out["access"] = access_from_decision(decision).model_dump(mode="json")
    out["provenance"] = (
        await provenance.build(creator_user_id=dep.get("user_id"))
    ).model_dump(mode="json")
    return out


@router.get("")
async def list_deployments(
    request: Request,
    trigger_type: Literal["api", "webhook"] | None = Query(default=None),
    enabled: Optional[bool] = Query(default=None),
    workflow_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Tenant-scoped list (RLS filters foreign-tenant rows). Supports
    ``trigger_type`` / ``enabled`` / ``workflow_id`` filters and standard
    pagination. Soft-deleted rows are excluded (repo level).
    """
    # Route handlers are also reused by the platform MCP layer and focused
    # unit tests. FastAPI's Query sentinel is only resolved on HTTP dispatch;
    # normalize it for direct in-process calls.
    q = q if isinstance(q, str) else None
    principal = principal_for_auth(ctx)
    context = context_for_auth(ctx, request)
    authorized_ids = await service.list_authorized_ids(
        principal,
        Action.VIEW_METADATA,
        ResourceType.DEPLOYMENT,
        context,
    )
    repo = DeploymentsRepo(session)
    items = await repo.list_for_tenant(
        deployment_ids=authorized_ids,
        trigger_type=trigger_type,
        enabled=enabled,
        wf_id=workflow_id,
        query=q,
        limit=limit,
        offset=offset,
    )
    total = await repo.count_for_tenant(
        deployment_ids=authorized_ids,
        trigger_type=trigger_type,
        enabled=enabled,
        wf_id=workflow_id,
        query=q,
    )
    summary = await repo.summary_for_tenant(deployment_ids=authorized_ids)
    if summary.get("last_invoked_at") is not None:
        summary["last_invoked_at"] = summary["last_invoked_at"].isoformat()
    resources = [
        _deployment_resource(ctx, item["id"]) for item in items
    ]
    decisions = await batch_resource_decisions(
        service,
        principal=principal,
        resources=resources,
        context=context,
    )
    provenance = ResourceProvenanceBuilder(session)
    output_items = [
        await _scrub_secret_fields(item, decisions[resource], provenance)
        for item, resource in zip(items, resources, strict=True)
    ]
    return {
        "items": output_items,
        "limit": limit,
        "offset": offset,
        "total": total,
        "summary": summary,
    }


@router.get("/{dep_id}")
async def get_deployment(
    dep_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Tenant-scoped single fetch. 404 when missing OR soft-deleted
    (the repo filters ``deleted_at IS NULL``). Secrets are scrubbed."""
    authorized = await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.VIEW,
    )
    dep = await DeploymentsRepo(session).get(dep_id)
    if not dep:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )
    return await _scrub_secret_fields(
        dep,
        authorized.decision,
        ResourceProvenanceBuilder(session),
    )


@router.patch("/{dep_id}")
async def patch_deployment(
    dep_id: uuid.UUID,
    body: PatchDeploymentBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Partial update. Each mutable field is validated INDEPENDENTLY here
    (vs T4's ``CreateDeploymentBody`` whole-object validators) because a
    PATCH may carry any subset, and Pydantic's per-field validators don't
    run when a field is omitted. 422 on invalid version_pin or
    rate_limit_qps. Soft-deleted rows surface as 404 (repo filter).
    """
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.UPDATE,
    )
    repo = DeploymentsRepo(session)
    dep = await repo.get(dep_id)
    if not dep:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )
    fields = body.model_dump(exclude_unset=True)
    if "version_pin" in fields and fields["version_pin"] is not None:
        if fields["version_pin"] not in _ALLOWED_VERSION_PIN:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="version_pin must be one of: head, major, specific",
            )
    if "rate_limit_qps" in fields and fields["rate_limit_qps"] is not None:
        if fields["rate_limit_qps"] < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="rate_limit_qps must be >= 0",
            )
    if any(fields.get(key, False) is None for key in ("name", "enabled", "mount_enabled", "rate_limit_qps", "version_pin")):
        raise HTTPException(422, "Deployment settings cannot be null.")
    if "name" in fields and (not fields["name"].strip() or len(fields["name"]) > 200):
        raise HTTPException(422, "name must contain 1 to 200 characters.")
    if {"version_pin", "pinned_major", "pinned_sub"} & fields.keys():
        selected = {**dep, **fields}
        if selected["version_pin"] == "head":
            fields.update(pinned_major=None, pinned_sub=None)
        elif selected["version_pin"] == "major":
            if not selected.get("pinned_major") or selected["pinned_major"] < 1:
                raise HTTPException(422, "A positive pinned_major is required.")
            fields["pinned_sub"] = None
        elif selected.get("pinned_major") is None or selected.get("pinned_sub") is None:
            raise HTTPException(422, "A complete pinned version is required.")
        await _authorize_workflow_deploy(request=request, ctx=ctx, service=service, workflow_id=dep["wf_id"],
                                        consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        await resolve_workflow(session, ctx.user_id, {**dep, **fields})
    authorized = await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    if fields:
        await repo.update(dep_id, **fields)
        if "enabled" in fields and dep.get("service_account_id") is not None:
            await ServiceAccountsRepo(session).set_status(
                uuid.UUID(str(dep["service_account_id"])),
                status="active" if fields["enabled"] else "disabled",
            )
        await session.flush()
    updated = await repo.get(dep_id)
    assert updated is not None
    return await _scrub_secret_fields(
        updated,
        authorized.decision,
        ResourceProvenanceBuilder(session),
    )


@router.delete("/{dep_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deployment(
    dep_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Soft delete. Sets ``deleted_at = now()`` AND ``enabled = FALSE``
    so the API and webhook resolvers stop returning the row
    in the same tick. The slug becomes reusable (migration-088 partial
    UNIQUE excludes soft-deleted rows). Idempotent."""
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.DELETE,
    )
    repo = DeploymentsRepo(session)
    dep = await repo.get(dep_id)
    if not dep:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )
    # Capture the name BEFORE the soft-delete so the audit snapshot is readable.
    name = dep.get("name") if isinstance(dep, dict) else getattr(dep, "name", None)
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    account_before = frozenset()
    if dep.get("service_account_id") is not None:
        account_id = uuid.UUID(str(dep["service_account_id"]))
        account_repo = ServiceAccountsRepo(session)
        credential_ids = tuple(
            str(value)
            for value in await account_repo.credential_ids(account_id)
        )
        account_before = service_account_edges(
            organization_id=ctx.active_organization_id,
            service_account_id=str(account_id),
            created_by=str(dep["user_id"]),
            owner_resource_type="deployment",
            owner_resource_id=str(dep_id),
            workflow_id=str(dep["wf_id"]),
            credential_ids=credential_ids,
        )
        await account_repo.set_status(account_id, status="deleted")
    await repo.soft_delete(dep_id)
    if dep.get("hmac_secret_ref"):
        await secret_service().destroy(
            session,
            secret_ref=dep["hmac_secret_ref"],
            tenant_id=ctx.active_organization_id,
        )
    await record_audit(
        session,
        action=actions.DEPLOYMENT_DELETE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=actions.TARGET_DEPLOYMENT,
        target_id=str(dep_id),
        target_name=name,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={},
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
        before=(
            resource_root_edges(
                organization_id=ctx.active_organization_id,
                object_type="deployment",
                object_id=str(dep_id),
                owner_relation="manager",
                owner_type="user",
                owner_id=str(dep["owner_id"]),
            )
            | account_before
        ),
        after=frozenset(),
        operation_id=uuid.uuid4().hex,
        source="deployment-delete",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{dep_id}/rotate-key")
async def rotate_key(
    dep_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Mint a fresh ``api_key`` plaintext + overwrite the stored hash.

    Single UPDATE replaces ``api_key_hash`` — the old key is destroyed
    atomically and the resolver immediately stops matching it.
    400 if the deployment is not ``trigger_type='api'`` (webhook secrets
    aren't rotated through this path; deletes the deployment + creates
    a new webhook deployment instead). 404 on missing / soft-deleted.
    """
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.MANAGE_SECRET,
    )
    repo = DeploymentsRepo(session)
    dep = await repo.get(dep_id)
    if not dep:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )
    if dep["trigger_type"] != "api":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rotate-key only applies to trigger_type=api",
        )
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.MANAGE_SECRET,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    plaintext, hashed = generate_api_key()
    await repo.update(dep_id, api_key_hash=hashed)
    # Audit: record the rotation but NEVER the new plaintext key.
    await record_audit(
        session,
        action=actions.DEPLOYMENT_KEY_ROTATE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=actions.TARGET_DEPLOYMENT,
        target_id=str(dep_id),
        target_name=dep.get("name") if isinstance(dep, dict)
        else getattr(dep, "name", None),
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={},
    )
    await session.flush()
    return {"api_key": plaintext}


@router.get(
    "/{dep_id}/access",
    response_model=DirectBindingListOut,
)
async def list_deployment_access(
    dep_id: uuid.UUID,
    request: Request,
    continuation_token: str = "",
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> DirectBindingListOut:
    _require_sharing_enabled()
    authorized = await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.MANAGE_ACCESS,
    )
    try:
        page = await service.list_bindings(
            principal_for_auth(ctx),
            authorized.resource,
            context_for_auth(
                ctx,
                request,
                consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
            ),
            continuation_token=continuation_token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DirectBindingListOut(
        items=[
            await direct_binding_out(session, item)
            for item in page.bindings
        ],
        continuation_token=page.continuation_token,
    )


async def _change_deployment_access(
    *,
    desired_present: bool,
    dep_id: uuid.UUID,
    body: DirectBindingIn,
    idempotency_key: str,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
) -> DirectBindingOut:
    _require_sharing_enabled()
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.MANAGE_ACCESS,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    binding = (
        binding_from_share_resolution(
            body.resolution_token,
            relation=body.relation,
            actor_user_id=ctx.user_id,
            session_id=ctx.session_id,
            resource=_deployment_resource(ctx, dep_id),
        )
        if desired_present and isinstance(body, DirectBindingGrantIn)
        else _binding_from_body(
            body,
            ctx=ctx,
            deployment_id=dep_id,
        )
    )
    try:
        result = await (
            service.grant(
                principal_for_auth(ctx),
                binding,
                context_for_auth(
                    ctx,
                    request,
                    consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
                ),
                idempotency_key=idempotency_key,
            )
            if desired_present
            else service.revoke(
                principal_for_auth(ctx),
                binding,
                context_for_auth(
                    ctx,
                    request,
                    consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
                ),
                idempotency_key=idempotency_key,
            )
        )
    except AuthorizationDeniedError as exc:
        raise HTTPException(404, "resource_not_found") from exc
    except AuthzMutationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _binding_out(result)


@router.post(
    "/{dep_id}/access",
    response_model=DirectBindingOut,
    status_code=status.HTTP_201_CREATED,
)
async def grant_deployment_access(
    dep_id: uuid.UUID,
    body: DirectBindingGrantIn,
    request: Request,
    idempotency_key: str = Header(
        min_length=1,
        max_length=200,
        alias="Idempotency-Key",
    ),
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    service: AuthzService = Depends(get_authz_service),
) -> DirectBindingOut:
    return await _change_deployment_access(
        desired_present=True,
        dep_id=dep_id,
        body=body,
        idempotency_key=idempotency_key,
        request=request,
        ctx=ctx,
        service=service,
    )


@router.delete(
    "/{dep_id}/access",
    response_model=DirectBindingOut,
)
async def revoke_deployment_access(
    dep_id: uuid.UUID,
    body: DirectBindingIn,
    request: Request,
    idempotency_key: str = Header(
        min_length=1,
        max_length=200,
        alias="Idempotency-Key",
    ),
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    service: AuthzService = Depends(get_authz_service),
) -> DirectBindingOut:
    return await _change_deployment_access(
        desired_present=False,
        dep_id=dep_id,
        body=body,
        idempotency_key=idempotency_key,
        request=request,
        ctx=ctx,
        service=service,
    )


# ---------------------------------------------------------------------------
# Deployments T12 — POST /api/v1/deployments/{id}/test-invoke
#
# Dashboard "test invoke" path: same execution as the public /invoke surface,
# but authenticated via the user's current session (NOT an api_key) and
# RLS-scoped to the caller's tenant. A user from another tenant looking up
# the same dep_id gets None back from the RLS-bound repo → 404.
# ---------------------------------------------------------------------------


@router.post("/{dep_id}/test-invoke")
async def test_invoke(
    dep_id: uuid.UUID,
    body: dict,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Spec §6 — test invoke from the dashboard. Same execution path as
    ``/invoke`` (load pinned version → run engine → drain stream), but the
    auth surface is the user's session cookie/bearer (resolved by
    ``current_user``) and the DB session is RLS-bound to their tenant.

    Foreign-tenant ``dep_id`` → repo returns ``None`` (RLS filtered) → 404.
    Disabled deployment → 404 (same status as the public ``/invoke`` to
    match operator expectations).
    """
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.EXECUTE,
    )
    repo = DeploymentsRepo(session)
    dep = await repo.get(dep_id)
    if dep is None or not dep["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.EXECUTE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )

    service_account_id = dep.get("service_account_id")
    lease = None
    if service_account_id is not None:
        try:
            lease = await ServiceAccountsRepo(session).require_active_lease(
                service_account_id=uuid.UUID(str(service_account_id)),
                owner_resource_type="deployment",
                owner_resource_id=str(dep_id),
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="deployment execution identity unavailable",
            ) from exc
    # The sandbox calls the Runtime Model Broker through a separate HTTP
    # request. Publish the running invocation in its own committed transaction
    # before starting the sandbox so that request can resolve the Invocation ->
    # Deployment authorization root and verify the execution is still active.
    # A separate short session also preserves the request session's RLS-bound
    # transaction for the authenticated control-plane work below.
    async with session_scope(tenant_id=ctx.tenant_id) as invocation_session:
        invocation_id = await DeploymentInvocationsRepo(
            invocation_session
        ).create(
            tenant_id=uuid.UUID(ctx.tenant_id),
            deployment_id=dep_id,
            wf_id=dep["wf_id"],
            trigger_type=dep["trigger_type"],
            source="test",
            status="running",
        )
    started = perf_counter()
    emit = getattr(request.state, "cli_deployment_progress", None)
    if emit is not None:
        await emit({"progress": {"deployment_id": str(dep_id), "execution_id": str(invocation_id),
            "status": "running", "message": "A real test invocation started. Do not submit it again."}})
    outputs: dict = {}
    errors: dict = {}
    fatal_http_exc: HTTPException | None = None
    try:
        approved_snapshot = getattr(request.state, "cli_deployment_snapshot", None)
        workflow_dict = approved_snapshot["workflow"] if approved_snapshot else await load_workflow_version(dep)
        execution_identity = (
            {
                "execution_principal_type": "service_account",
                "execution_principal_id": str(lease.service_account_id),
                "execution_principal_generation": lease.generation,
            }
            if lease is not None else {}
        )
        outputs, errors, exec_secs = await asyncio.to_thread(
            run_workflow_sandboxed_sync,
            workflow_id=dep["wf_id"], inputs=body,
            tenant_id=ctx.tenant_id,
            user_id=str(lease.created_by if lease is not None else dep["user_id"]),
            run_id=str(invocation_id),
            workflow_dict=workflow_dict,
            mount_enabled=approved_snapshot["mount_enabled"] if approved_snapshot else dep.get("mount_enabled", True),
            execution_resource_type=ResourceType.DEPLOYMENT_INVOCATION.value,
            **execution_identity,
        )
        exec_time_ms = exec_secs * 1000.0
    except HTTPException as exc:
        exec_time_ms = (perf_counter() - started) * 1000.0
        errors = {"__top__": str(exc.detail)}
        fatal_http_exc = exc
    except Exception as exc:
        exec_time_ms = (perf_counter() - started) * 1000.0
        errors = {"__top__": f"{type(exc).__name__}: {exc}"}
    async with session_scope(tenant_id=ctx.tenant_id) as invocation_session:
        await DeploymentInvocationsRepo(invocation_session).mark_terminal(
            invocation_id,
            status="failed" if errors else "succeeded",
            latency_ms=exec_time_ms,
            error="execution_failed" if errors else None,
            result_summary={
                "output_count": len(outputs) if isinstance(outputs, dict) else 0,
                "error_count": len(errors) if isinstance(errors, dict) else 0,
            },
        )
    if fatal_http_exc is not None and emit is None:
        raise fatal_http_exc
    return {
        "outputs": outputs,
        "errors": errors,
        "exec_time_ms": exec_time_ms,
        "execution_id": str(invocation_id),
        "status": "failed" if errors else "succeeded",
    }


# ---------------------------------------------------------------------------
# Deployments T13 — GET /api/v1/deployments/{id}/metrics + /history
#
# Deployment invocation observability is intentionally owned by Deployment,
# not the global Task Center. The Task table now tracks only user-managed
# batch/schedule work, so these endpoints keep their response shape but no
# longer read from ``tasks``. A deployment-run log store should back these
# endpoints when online-serving observability is implemented.
# ---------------------------------------------------------------------------


@router.get("/{dep_id}/metrics")
async def metrics(
    dep_id: uuid.UUID,
    request: Request,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(...),
    bucket: str = Query(default="hour"),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Bucketed metrics for deployment invocation history.

    Reads the deployment-owned invocation store. Deployment observability does
    not use Task rows.
    """
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.INSPECT_RUNS,
    )
    if bucket not in ("hour", "day"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bucket must be 'hour' or 'day'",
        )

    # First gate: ensure caller can see this deployment (RLS-bound repo
    # returns ``None`` for foreign-tenant rows → 404, same shape as a
    # missing deployment so we don't leak existence).
    dep = await DeploymentsRepo(session).get(dep_id)
    if dep is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )

    series = await DeploymentInvocationsRepo(session).metrics(
        deployment_id=dep_id,
        from_=from_,
        to=to,
        bucket=bucket,
    )
    return {
        "series": series,
        "bucket": bucket,
        "from": from_.isoformat(),
        "to": to.isoformat(),
    }


@router.get("/{dep_id}/history")
async def history(
    dep_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None),
    status_filter: Optional[List[str]] = Query(default=None, alias="status"),
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Cursor-paginated deployment invocation history.

    Reads the deployment-owned invocation store; Task Center rows are not used
    for online-serving observability.
    """
    await _authorize_deployment(
        request=request,
        ctx=ctx,
        service=service,
        deployment_id=dep_id,
        action=Action.INSPECT_RUNS,
    )
    dep = await DeploymentsRepo(session).get(dep_id)
    if dep is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )

    if any(value is not None and value.utcoffset() is None for value in (from_, to)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from and to must include a timezone offset",
        )
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from must be before or equal to to",
        )

    return await DeploymentInvocationsRepo(session).history(
        deployment_id=dep_id,
        limit=limit,
        cursor=cursor,
        statuses=status_filter,
        from_=from_,
        to=to,
        descending=order == "desc",
    )
