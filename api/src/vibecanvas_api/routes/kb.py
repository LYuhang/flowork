"""Knowledge package REST API — packages, files, sharing, and search.

Mirrors the auth + session shape of every other business route in this
package: ``current_user`` resolves the bearer token (AuthContext from
``auth/deps.py``); ``tenant_db`` opens a tenant-bound session with
``app.tenant_id`` set, so Postgres FORCE RLS isolates every read/write
to the caller's tenant automatically.

Upload route ordering (spec sec 8 ``Upload route ordering``):

  Step 1: validate file (size <= 50 MB, parser_type via filename + MIME)
  Step 2: DB INSERT kb_files (status='pending', object_store_key=NULL)
  Step 3: write blob to object_store
  Step 4: UPDATE kb_files.object_store_key
  Step 5: enqueue a durable indexing workflow

If any step fails, ``kb_orphan_reconciler`` (Case A: step 3 failure,
Case B: enqueue failure) sweeps the orphans on a 5-minute cadence.

Auth: every route is gated by ``Depends(current_user)``. RLS handles
tenant isolation automatically — there is no manual ``tenant_id`` /
``user_id`` filter in any query here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
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
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaUnavailableError,
)
from vibecanvas_api.authorization.share_resolution import (
    binding_from_share_resolution,
)
from vibecanvas_api.authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    resource_root_edges,
)
from vibecanvas_api.authorization.service import (
    AuthorizationDeniedError,
    AuthzService,
    batch_resource_decisions,
)
from vibecanvas_api.authorization.types import (
    Action,
    AuthorizationCheck,
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
from vibecanvas_api.security.upload_scanner import require_clean_upload
from vibecanvas_api.schemas.access import (
    DirectBindingGrantIn,
    DirectBindingIn,
    DirectBindingListOut,
    DirectBindingOut,
    ResourceAccessOut,
    ResourceProvenanceOut,
    access_from_decision,
    decision_allows_content,
)
from vibecanvas_api.services.kb_search import (
    EncryptedKbSearchLimitError,
    KbSearchService,
)
from vibecanvas_api.services.background_queue import enqueue_background_job_async
from vibecanvas_api.services.knowledge_packages import (
    MAX_PACKAGE_BYTES,
    PackageFile,
    enqueue_package_indexing,
    normalize_imported_package,
    normalize_package_path,
    package_files_from_zip,
    replace_package,
    resolve_package_content_type,
)
from vibecanvas_api.services.access_presentation import direct_binding_out
from vibecanvas_api.services.object_store import (
    get_object_store,
)
from vibecanvas_api.services.parsers import detect_parser_type
from vibecanvas_api.services.queue_routing import route_for
from vibecanvas_api.services.resource_provenance import (
    ResourceProvenanceBuilder,
)
from vibecanvas_api.storage.repo_kb import KbRepo


# Spec sec 8: hard cap on a single uploaded file. Anything bigger
# blows the indexer's parse + chunk + embed memory + token budget.
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024


router = APIRouter(prefix="/api/v1/kb", tags=["kb"])


# ----------------------------------------------------------------- schemas


class KbCreate(BaseModel):
    """Body for ``POST /kb``. ``extra='forbid'`` blocks smuggled fields
    (``tenant_id`` / ``user_id`` / ``id`` / retrieval internals);
    those come from ``AuthContext`` or a server-side default."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)


class KbUpdate(BaseModel):
    """Body for ``PATCH /kb/:id``. All fields optional + ``extra='forbid'``
    so the same trust boundary applies."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)


class KbOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    summary: Optional[str]
    retrieval_strategy: str = "agentic_lexical"
    package_version: int
    created_at: str
    updated_at: str
    access: ResourceAccessOut
    provenance: ResourceProvenanceOut


class KbListOut(KbOut):
    file_count: int = 0
    chunk_count: int = 0
    stored_count: int = 0
    pending_count: int = 0
    indexing_count: int = 0
    indexed_count: int = 0
    failed_count: int = 0
    latest_updated_at: str


class KbDetailOut(KbOut):
    file_count: int
    chunk_count: int
    latest_updated_at: str  # max(kb.updated_at, max(file.updated_at)) — for frontend polling


class KbFileOut(BaseModel):
    id: str
    name: str
    parser_type: str
    mime_type: str
    file_size: int
    status: str
    error_message: Optional[str]
    chunk_count: int
    created_at: str
    access: ResourceAccessOut
    provenance: ResourceProvenanceOut


class SearchRequest(BaseModel):
    """Body for encrypted grep-style Knowledge search."""

    model_config = ConfigDict(extra="forbid")

    kb_ids: list[str] = Field(..., min_length=1, max_length=50)
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


# ----------------------------------------------------------------- helpers


async def _rebind_tenant_guc(
    session: AsyncSession, tenant_id: str,
) -> None:
    """Re-set the transaction-local ``app.tenant_id`` GUC after a commit.

    ``session_scope(tenant_id=...)`` sets this GUC with ``is_local=true``
    on the FIRST transaction; once we commit it, the GUC is gone and the
    next statement runs against FORCE RLS with no tenant context — RLS
    expressions ``current_setting('app.tenant_id', true)::uuid`` then
    blow up casting an empty string to UUID. The upload route commits
    multiple times (DB-first ordering: row durable BEFORE put_bytes,
    key durable BEFORE tasks insert, tasks durable BEFORE send_task), so
    each commit MUST be followed by a rebind on the SAME session.
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"),
        {"t": tenant_id},
    )


def _knowledge_base_resource(
    ctx: AuthContext,
    knowledge_base_id: uuid.UUID | str,
) -> ResourceRef:
    return ResourceRef(
        ResourceType.KNOWLEDGE_BASE,
        str(knowledge_base_id),
        ctx.active_organization_id,
    )


def _knowledge_base_file_resource(
    ctx: AuthContext,
    file_id: uuid.UUID | str,
) -> ResourceRef:
    return ResourceRef(
        ResourceType.KNOWLEDGE_BASE_FILE,
        str(file_id),
        ctx.active_organization_id,
    )


async def _authorize_knowledge_base(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    knowledge_base_id: uuid.UUID | str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = _knowledge_base_resource(ctx, knowledge_base_id)
    decision = await service.check(
        principal_for_auth(ctx),
        action,
        resource,
        context_for_auth(ctx, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_not_found",
        )
    return AuthorizedResource(resource=resource, decision=decision)


async def _authorize_knowledge_base_file(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    file_id: uuid.UUID | str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = _knowledge_base_file_resource(ctx, file_id)
    decision = await service.check(
        principal_for_auth(ctx),
        action,
        resource,
        context_for_auth(ctx, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_file_not_found",
        )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="resource_not_found",
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
    knowledge_base_id: uuid.UUID,
) -> RelationshipBinding:
    return RelationshipBinding(
        subject=RelationshipSubject(
            type=RelationshipSubjectType(body.subject_type),
            id=body.subject_id,
            relation=body.subject_relation,
        ),
        relation=body.relation,
        resource=_knowledge_base_resource(ctx, knowledge_base_id),
    )


def _require_sharing_enabled() -> None:
    if not config.resource_sharing_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="resource_not_found",
        )


async def _kb_to_out(
    kb,
    decision: Decision,
    provenance: ResourceProvenanceBuilder,
) -> KbOut:
    """KnowledgeBase ORM -> KbOut. Coerces UUID + datetime to JSON-safe
    strings. Centralised so every read endpoint emits identical shape."""
    return KbOut(
        id=str(kb.id),
        name=kb.name,
        description=(
            kb.description if decision_allows_content(decision) else None
        ),
        summary=(kb.summary if decision_allows_content(decision) else None),
        retrieval_strategy="agentic_lexical",
        package_version=kb.package_version,
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat(),
        access=access_from_decision(decision),
        provenance=await provenance.build(creator_user_id=kb.user_id),
    )


async def _kb_file_to_out(
    f,
    decision: Decision,
    provenance: ResourceProvenanceBuilder,
) -> KbFileOut:
    return KbFileOut(
        id=str(f.id),
        name=f.name,
        parser_type=f.parser_type,
        mime_type=f.mime_type,
        file_size=f.file_size,
        status=f.status,
        error_message=f.error_message,
        chunk_count=f.chunk_count,
        created_at=f.created_at.isoformat(),
        access=access_from_decision(decision),
        provenance=await provenance.build(
            creator_user_id=f.user_id,
            origin_type="uploaded",
        ),
    )


# ----------------------------------------------------------------- KB CRUD


async def _create_knowledge_package(
    *,
    body: KbCreate,
    package_files: list[PackageFile],
    derive_index: bool,
    request: Request,
    ctx: AuthContext,
    session: AsyncSession,
    service: AuthzService,
) -> KbOut:
    """Persist one authoritative package and its authorization projection."""
    repo = KbRepo(session)
    try:
        kb = await repo.create_kb(
            tenant_id=uuid.UUID(ctx.active_organization_id),
            user_id=uuid.UUID(ctx.user_id),
            name=body.name,
            description=body.description,
        )
        await session.flush()
        _, pending_index_files = await replace_package(
            session,
            kb_id=kb.id,
            actor_user_id=uuid.UUID(ctx.user_id),
            expected_version=1,
            files=package_files,
            increment_version=False,
            derive_index=derive_index,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="kb_name_conflict",
        ) from exc
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
            object_type="knowledge_base",
            object_id=str(kb.id),
            owner_relation="manager",
            owner_type="user",
            owner_id=ctx.user_id,
        ),
        operation_id=uuid.uuid4().hex,
        source="knowledge-base-create",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await enqueue_package_indexing(
        tenant_id=str(kb.tenant_id),
        user_id=ctx.user_id,
        file_ids=pending_index_files,
    )
    await _rebind_tenant_guc(session, ctx.active_organization_id)
    decision = await service.check(
        principal_for_auth(ctx),
        Action.VIEW_METADATA,
        _knowledge_base_resource(ctx, kb.id),
        context_for_auth(
            ctx,
            request,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        ),
    )
    if not decision.allowed:
        raise OpenFgaUnavailableError(
            "authorization_projection_not_visible"
        )
    return await _kb_to_out(
        kb,
        decision,
        ResourceProvenanceBuilder(session),
    )


@router.post(
    "",
    response_model=KbOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_kb(
    body: KbCreate,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Create a new knowledge base under the current tenant.

    409 on duplicate name (partial UNIQUE on the active tenant/name digest).
    """
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    readme = (
        f"# {body.name}\n\n"
        f"{body.description or 'Describe the purpose and scope of this knowledge package.'}\n\n"
        "## Directory\n\n"
        "- `README.md` — package purpose, scope, structure, and file guide.\n"
    ).encode("utf-8")
    return await _create_knowledge_package(
        body=body,
        package_files=[PackageFile("README.md", readme, "text/markdown")],
        derive_index=False,
        request=request,
        ctx=ctx,
        session=session,
        service=service,
    )


@router.post(
    "/import",
    response_model=KbOut,
    status_code=status.HTTP_201_CREATED,
)
async def import_kb(
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(default=None),
    archive: Optional[UploadFile] = File(default=None),
    files: Optional[list[UploadFile]] = File(default=None),
    paths: Optional[list[str]] = Form(default=None),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Create Knowledge from one complete folder upload or ZIP archive."""
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    try:
        body = KbCreate(name=name, description=description or None)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="knowledge_package_metadata_invalid",
        ) from exc
    supplied_files = files or []
    supplied_paths = paths or []
    if (archive is None) == (not supplied_files):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="knowledge_package_source_required",
        )
    try:
        if archive is not None:
            blob = await archive.read(MAX_PACKAGE_BYTES + 1)
            if len(blob) > MAX_PACKAGE_BYTES:
                raise ValueError("Knowledge ZIP exceeds the upload limit")
            await require_clean_upload(blob)
            package_files = package_files_from_zip(blob)
        else:
            if len(supplied_files) != len(supplied_paths):
                raise ValueError("Every uploaded file requires a relative path")
            pending: list[PackageFile] = []
            for upload, path in zip(
                supplied_files, supplied_paths, strict=True,
            ):
                data = await upload.read(MAX_FILE_SIZE_BYTES + 1)
                if len(data) > MAX_FILE_SIZE_BYTES:
                    raise ValueError("Knowledge package contains an oversized file")
                await require_clean_upload(data)
                pending.append(PackageFile(path, data, upload.content_type or ""))
            package_files = normalize_imported_package(pending)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "knowledge_package_invalid", "message": str(exc)},
        ) from exc
    return await _create_knowledge_package(
        body=body,
        package_files=package_files,
        derive_index=True,
        request=request,
        ctx=ctx,
        session=session,
        service=service,
    )


@router.get("", response_model=list[KbListOut])
async def list_kbs(
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """List only KBs visible to the caller, intersected with tenant SQL."""
    principal = principal_for_auth(ctx)
    context = context_for_auth(ctx, request)
    authorized_ids = await service.list_authorized_ids(
        principal,
        Action.VIEW_METADATA,
        ResourceType.KNOWLEDGE_BASE,
        context,
    )
    repo = KbRepo(session)
    kbs = await repo.list_active(authorized_ids)
    stats_by_id = await repo.list_file_stats([kb.id for kb in kbs])
    resources = [
        _knowledge_base_resource(ctx, kb.id)
        for kb in kbs
    ]
    decisions = await batch_resource_decisions(
        service,
        principal=principal,
        resources=resources,
        context=context,
    )
    result: list[KbListOut] = []
    provenance = ResourceProvenanceBuilder(session)
    for kb, resource in zip(kbs, resources, strict=True):
        base = await _kb_to_out(kb, decisions[resource], provenance)
        stats = stats_by_id.get(str(kb.id), {})
        latest = stats.get("latest_updated_at")
        if not isinstance(latest, datetime) or latest < kb.updated_at:
            latest = kb.updated_at
        result.append(KbListOut(
            **base.model_dump(),
            file_count=int(stats.get("file_count", 0)),
            chunk_count=int(stats.get("chunk_count", 0)),
            stored_count=int(stats.get("stored_count", 0)),
            pending_count=int(stats.get("pending_count", 0)),
            indexing_count=int(stats.get("indexing_count", 0)),
            indexed_count=int(stats.get("indexed_count", 0)),
            failed_count=int(stats.get("failed_count", 0)),
            latest_updated_at=latest.isoformat(),
        ))
    return result


@router.get("/{kb_id}", response_model=KbDetailOut)
async def get_kb(
    kb_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Fetch one KB + its aggregate counts (file_count, chunk_count).
    404 if missing or soft-deleted."""
    authorized = await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.VIEW,
    )
    repo = KbRepo(session)
    kb = await repo.get_active(kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_not_found",
        )
    files = await repo.list_files(kb_id)
    chunk_count = await repo.count_chunks(kb_id)
    # latest_updated_at = max(kb.updated_at, latest file.updated_at). Frontend
    # polling uses this to detect activity (KB rename, file added/indexed/deleted).
    latest = kb.updated_at
    for f in files:
        if f.updated_at > latest:
            latest = f.updated_at
    return KbDetailOut(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        summary=kb.summary,
        retrieval_strategy="agentic_lexical",
        package_version=kb.package_version,
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat(),
        file_count=len(files),
        chunk_count=chunk_count,
        latest_updated_at=latest.isoformat(),
        access=access_from_decision(authorized.decision),
        provenance=await ResourceProvenanceBuilder(session).build(
            creator_user_id=kb.user_id,
        ),
    )


@router.patch("/{kb_id}", response_model=KbOut)
async def update_kb(
    kb_id: uuid.UUID,
    body: KbUpdate,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Patch ``name`` / ``description``. 404 if missing; 409 on
    duplicate name."""
    await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.UPDATE,
    )
    repo = KbRepo(session)
    kb = await repo.get_active(kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_not_found",
        )
    authorized = await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    try:
        await repo.update_kb(
            kb_id, name=body.name, description=body.description,
        )
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="kb_name_conflict",
        )
    # Re-read so we pick up the trigger-bumped ``updated_at``.
    kb = await repo.get_active(kb_id)
    return await _kb_to_out(
        kb,
        authorized.decision,
        ResourceProvenanceBuilder(session),
    )


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(
    kb_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Soft-delete a KB (Tier 1; the GC sweeper hard-deletes 30 days later).

    Reject the delete with 409 if ANY file is currently indexing — the
    indexer would race a CASCADE that hasn't fired yet, and the partial
    chunk writes would be orphaned (spec sec 10).
    """
    await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.DELETE,
    )
    repo = KbRepo(session)
    kb = await repo.get_active(kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_not_found",
        )
    files = await repo.list_files(kb_id)
    if any(f.status == "indexing" for f in files):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="kb_delete_in_progress",
        )
    await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    # Capture the kb name BEFORE the soft-delete for the audit snapshot.
    kb_name = kb.name if not isinstance(kb, dict) else kb.get("name")
    await repo.soft_delete_kb(kb_id)
    await record_audit(
        session,
        action=actions.KB_DELETE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=actions.TARGET_KB,
        target_id=str(kb_id),
        target_name=kb_name,
        outcome="success",
        audit_ctx=extract_request_audit_context(request) if request else None,
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
        before=resource_root_edges(
            organization_id=ctx.active_organization_id,
            object_type="knowledge_base",
            object_id=str(kb_id),
            owner_relation="manager",
            owner_type="user",
            owner_id=str(kb.user_id),
        ),
        after=frozenset(),
        operation_id=uuid.uuid4().hex,
        source="knowledge-base-delete",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------- file CRUD


@router.post("/{kb_id}/files")
async def upload_file(
    kb_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Add one raw file to a Knowledge package.

    Supported document types are queued for the replaceable search index.
    Other file types remain available as authoritative package files with a
    ``stored`` status.
    Errors:
      * 413 ``kb_file_too_large`` — payload > 50 MB.
      * 404 ``kb_not_found`` — KB missing or soft-deleted.
    """
    await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.UPDATE,
    )
    repo = KbRepo(session)
    kb = await repo.get_active(kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_not_found",
        )

    # Step 1: validate.
    blob = await file.read()
    if len(blob) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="kb_file_too_large",
        )
    await require_clean_upload(blob)
    try:
        package_path = normalize_package_path(file.filename or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="knowledge_package_path_invalid",
        ) from exc
    if any(
        existing.name.casefold() == package_path.casefold()
        for existing in await repo.list_files(kb_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="knowledge_package_path_exists",
        )
    mime_type = resolve_package_content_type(
        package_path,
        blob,
        file.content_type,
    )
    parser_type = detect_parser_type(package_path, mime_type) or "binary"
    content_hash = hashlib.sha256(blob).hexdigest()

    await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )

    kb_file = await repo.create_file(
            kb_id=kb_id,
            tenant_id=kb.tenant_id,
            user_id=uuid.UUID(ctx.user_id),
            name=package_path,
            parser_type=parser_type,
            mime_type=mime_type,
            file_size=len(blob),
            content_hash=content_hash,
            status="pending" if parser_type != "binary" else "stored",
            object_store_key=None,
        )
    await session.commit()
    await _rebind_tenant_guc(session, ctx.active_organization_id)

    # Step 3: write blob to object_store (BLOCKING — to_thread).
    # ``put_bytes`` returns a URI; we store the BARE KEY on the row
    # (matches the indexer's ``fetch_bytes(key)`` contract — see
    # services/object_store.py).
    # Object keys are opaque identifiers. The private display filename lives
    # only inside the file envelope and therefore cannot leak through storage
    # listings, logs, or signed URLs.
    object_key = f"kb/{kb.tenant_id}/{kb_id}/{kb_file.id}/content"
    store = get_object_store()
    await asyncio.to_thread(
        store.put_bytes, object_key, blob,
        mime_type,
    )

    # Step 4: update DB row with the key.
    await repo.set_object_store_key(kb_file.id, object_key)
    await repo.bump_package_version(kb_id)
    await session.commit()
    await _rebind_tenant_guc(session, ctx.active_organization_id)

    # Step 5: enqueue durable work. KB indexing state is tracked by kb_files, not by
    # the platform Task center.
    task_id = uuid.uuid4() if parser_type != "binary" else None
    if task_id is not None:
        await enqueue_background_job_async(
            "kb.index_file",
            job_id=str(task_id),
            queue=route_for("kb_index_file"),
            kwargs={"file_id": str(kb_file.id)},
        )
    return {
        "file_id": str(kb_file.id),
        "task_id": str(task_id) if task_id else None,
        "status": kb_file.status,
    }


@router.get("/{kb_id}/files", response_model=list[KbFileOut])
async def list_files(
    kb_id: uuid.UUID,
    request: Request,
    file_status: Optional[str] = Query(default=None, alias="status"),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """List files in a KB; optional ``?status=`` filter. RLS already
    scopes to the tenant; the repo also filters ``deleted_at IS NULL``.

    Local var is ``file_status`` to avoid shadowing the ``fastapi.status``
    module imported at file top; the public query-param name stays
    ``status`` per spec sec 8 contract via ``Query(alias="status")``.
    """
    authorized = await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.VIEW,
    )
    repo = KbRepo(session)
    kb = await repo.get_active(kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_not_found",
        )
    files = await repo.list_files(kb_id, status=file_status)
    provenance = ResourceProvenanceBuilder(session)
    return [
        await _kb_file_to_out(f, authorized.decision, provenance)
        for f in files
    ]


@router.get("/{kb_id}/files/{file_id}/raw")
async def get_file_raw(
    kb_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Stream one authoritative package file after content authorization."""
    await _authorize_knowledge_base_file(
        request=request, ctx=ctx, service=service,
        file_id=file_id, action=Action.VIEW,
    )
    file_obj = await KbRepo(session).get_file(file_id)
    if (
        file_obj is None
        or file_obj.kb_id != kb_id
        or not file_obj.object_store_key
    ):
        raise HTTPException(status_code=404, detail="kb_file_not_found")
    try:
        blob = await asyncio.to_thread(
            get_object_store().fetch_bytes,
            file_obj.object_store_key,
        )
    except (KeyError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="kb_file_content_missing") from exc
    return Response(
        content=blob,
        media_type=file_obj.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{file_obj.id}"',
            "Cache-Control": "private, max-age=60",
        },
    )


@router.delete(
    "/{kb_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_file(
    kb_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Soft-delete one file. Chunks remain physically present until the
    GC sweeper hard-deletes 30 days later (spec sec 4.6); the search SQL
    filters ``deleted_at IS NULL`` so the file disappears from results
    immediately.

    Pre-checks the file exists + belongs to ``kb_id`` (RLS already enforces
    tenant scope, but cross-KB ownership is application-layer). Missing or
    foreign file → 404 ``kb_file_not_found``.
    """
    await _authorize_knowledge_base_file(
        request=request,
        ctx=ctx,
        service=service,
        file_id=file_id,
        action=Action.DELETE,
    )
    repo = KbRepo(session)
    file_obj = await repo.get_file(file_id)
    if not file_obj or file_obj.kb_id != kb_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_file_not_found",
        )
    if file_obj.name.casefold() == "readme.md":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="knowledge_root_readme_required",
        )
    await _authorize_knowledge_base_file(
        request=request,
        ctx=ctx,
        service=service,
        file_id=file_id,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    await repo.soft_delete_file(file_id)
    await repo.bump_package_version(kb_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------- sharing


@router.get(
    "/{kb_id}/access",
    response_model=DirectBindingListOut,
)
async def list_kb_access(
    kb_id: uuid.UUID,
    request: Request,
    continuation_token: str = "",
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> DirectBindingListOut:
    _require_sharing_enabled()
    authorized = await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
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


async def _change_kb_access(
    *,
    desired_present: bool,
    kb_id: uuid.UUID,
    body: DirectBindingIn,
    idempotency_key: str,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
) -> DirectBindingOut:
    _require_sharing_enabled()
    await _authorize_knowledge_base(
        request=request,
        ctx=ctx,
        service=service,
        knowledge_base_id=kb_id,
        action=Action.MANAGE_ACCESS,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    binding = (
        binding_from_share_resolution(
            body.resolution_token,
            relation=body.relation,
            actor_user_id=ctx.user_id,
            session_id=ctx.session_id,
            resource=_knowledge_base_resource(ctx, kb_id),
        )
        if desired_present and isinstance(body, DirectBindingGrantIn)
        else _binding_from_body(
            body,
            ctx=ctx,
            knowledge_base_id=kb_id,
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="resource_not_found",
        ) from exc
    except AuthzMutationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _binding_out(result)


@router.post(
    "/{kb_id}/access",
    response_model=DirectBindingOut,
    status_code=status.HTTP_201_CREATED,
)
async def grant_kb_access(
    kb_id: uuid.UUID,
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
    return await _change_kb_access(
        desired_present=True,
        kb_id=kb_id,
        body=body,
        idempotency_key=idempotency_key,
        request=request,
        ctx=ctx,
        service=service,
    )


@router.delete(
    "/{kb_id}/access",
    response_model=DirectBindingOut,
)
async def revoke_kb_access(
    kb_id: uuid.UUID,
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
    return await _change_kb_access(
        desired_present=False,
        kb_id=kb_id,
        body=body,
        idempotency_key=idempotency_key,
        request=request,
        ctx=ctx,
        service=service,
    )


# ----------------------------------------------------------------- search


@router.post("/search")
async def search(
    body: SearchRequest,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Encrypted lexical search over one or more KBs. RLS already scopes to the
    tenant; the search SQL also filters ``deleted_at IS NULL`` on both
    ``kb_files`` and ``knowledge_bases`` (spec sec 6.2)."""
    normalized_ids: list[str] = []
    for value in dict.fromkeys(body.kb_ids):
        try:
            normalized_ids.append(str(uuid.UUID(value)))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="kb_not_found",
            ) from exc
    principal = principal_for_auth(ctx)
    context = context_for_auth(ctx, request)
    resources = [
        _knowledge_base_resource(ctx, value)
        for value in normalized_ids
    ]
    decisions = await service.batch_check(tuple(
        AuthorizationCheck(
            principal=principal,
            action=Action.USE,
            resource=resource,
            context=context,
        )
        for resource in resources
    ))
    if any(not decision.allowed for decision in decisions):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_not_found",
        )
    tenant_rows = await KbRepo(session).list_active(normalized_ids)
    if {str(row.id) for row in tenant_rows} != set(normalized_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="kb_not_found",
        )
    svc = KbSearchService(session)
    try:
        results = await svc.search_async(
            kb_ids=normalized_ids,
            query=body.query,
            top_k=body.top_k,
        )
    except EncryptedKbSearchLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": str(exc)},
        ) from exc
    return {"results": [r.model_dump() for r in results]}
