"""Workflows + versions + edits + check + prompt-history.

Business logic ported from legacy demo/handlers/workflow.py:18-200 and
demo/handlers/workspace.py:84-280.

The old process-wide file-backed repository cache
replaced by a per-request session-scoped ``WorkflowRepo`` injected via
``Depends(get_workflow_repo)``. Every repo method is now ``async``; the
request/response contract is unchanged.

The per-workflow in-process ``asyncio.Lock``
(``get_wf_lock``) is removed from the commit / edits / major-version /
undo / redo / checkout paths. It never covered the durable write —
the COMMIT happens in FastAPI dependency teardown (``get_db``), after
the lock released, so two concurrent commits could regress the HEAD
pointer under READ COMMITTED. Serialization is now at the DB:
``SELECT ... FOR UPDATE`` on the ``Workflow`` row inside the
head-mutating repo methods (the row lock is held by Postgres until
the teardown commit, so it genuinely covers the durable write).
"""

from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import actions
from ..audit.context import extract_request_audit_context
from ..audit.service import record_audit
from ..auth.deps import (
    AuthContext,
    current_user,
    require_recent_step_up,
    tenant_db,
)
from ..authorization.dependencies import (
    authorize_resource,
    context_for_auth,
    get_authz_service,
    mutation_coordinator_for_request,
    principal_for_auth,
)
from ..authorization.openfga_client import OpenFgaUnavailableError
from ..authorization.share_resolution import binding_from_share_resolution
from ..authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    resource_root_edges,
    service_account_edges,
)
from ..authorization.service import (
    AuthorizationDeniedError,
    AuthzService,
    batch_resource_decisions,
)
from ..authorization.mutations import AuthzMutationError
from ..authorization.types import (
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
from ..config import config
from ..schemas.access import (
    DirectBindingGrantIn,
    DirectBindingIn,
    DirectBindingListOut,
    DirectBindingOut,
    access_from_decision,
    decision_allows_content,
)
from ..services.background_queue import enqueue_background_job_async
from ..services.batch_output import build_output_sink
from ..services.access_presentation import direct_binding_out
from ..services.object_store import get_object_store
from ..services.resource_provenance import ResourceProvenanceBuilder
from ..services.service_account_credentials import bind_workflow_credentials
from ..services.sandbox.manager import get_sandbox_manager
from ..services.user_mount_workspace import mount_scope_id as _mount_scope_id
from ..schemas.pagination import Page, PageRequest
from ..schemas.workflow import (
    CheckRequest, CheckResponse, CheckoutRequest, CommitRequest, EditsRequest,
    EditsResponse, PromptHistoryOut, WorkflowCreate, WorkflowMetaOut,
    WorkflowMetaPatch, WorkflowSnapshotOut,
)
from ..storage import stop_registry
from ..storage.execution_repo import running_execution_ids
from ..storage.repo_tasks import TasksRepo
from ..storage.repo_service_accounts import ServiceAccountsRepo
from ..storage.vfs_run_repo import VfsRunRepo
from ..storage.vfs_store import VfsRepo
from ..storage.workflow_repo import WorkflowRepo
from ..utils.updater import WorkflowUpdater
from .deps import get_workflow_repo

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


async def _workflow_sandbox_status_payload(
    *,
    session: AsyncSession,
    tenant_id: str,
    user_id: str,
    wf_id: str,
) -> dict:
    status_payload = await get_sandbox_manager().status(tenant_id, wf_id)
    running_exec_ids = running_execution_ids(wf_id)
    if running_exec_ids:
        status_payload = {
            **status_payload,
            "status": "running",
            "activity_state": "busy",
            "idle_elapsed_s": 0.0,
            "idle_for_s": 0.0,
            "ttl_paused": True,
            "ttl_remaining_s": None,
            "active_execution_ids": running_exec_ids,
        }
    return {
        "wf_id": wf_id,
        "scope_id": wf_id,
        "mount_scope_id": _mount_scope_id(user_id),
        **status_payload,
    }


async def _meta_to_out(
    meta: dict,
    decision: Decision,
    provenance: ResourceProvenanceBuilder,
) -> WorkflowMetaOut:
    # Organization administrators and auditors intentionally receive
    # ``view_metadata`` without ``view``.  A workflow description and tags are
    # user-authored private content, not safe directory metadata, so the list
    # projection must not decrypt/expose them to metadata-only principals.
    # Keep the stable display name/version/timestamps needed for inventory and
    # audit review while the content endpoint remains a 404.
    can_view_content = decision_allows_content(decision)
    return WorkflowMetaOut(
        wf_id=meta["wf_id"],
        workflow_name=meta.get("workflow_name", ""),
        description=meta.get("description", "") if can_view_content else "",
        active_v=meta.get("active_v", 1),
        active_sv=meta.get("active_sv", 0),
        updated_at=meta.get("updated_at", 0.0),
        created_at=meta.get("created_at", 0.0),
        tags=meta.get("tags", []) if can_view_content else [],
        access=access_from_decision(decision),
        provenance=await provenance.build(
            creator_user_id=meta.get("creator"),
        ),
    )


def _workflow_resource(auth: AuthContext, wf_id: str) -> ResourceRef:
    return ResourceRef(
        ResourceType.WORKFLOW,
        wf_id,
        auth.active_organization_id,
    )


async def _authorize_workflow(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    wf_id: str,
    action: Action,
) -> AuthorizedResource:
    return await authorize_resource(
        request=request,
        auth=auth,
        service=service,
        resource=_workflow_resource(auth, wf_id),
        action=action,
    )


async def _rebind_request_organization(
    session: AsyncSession,
    auth: AuthContext,
) -> None:
    """Reapply the transaction-local RLS scope after an explicit commit."""
    await session.execute(
        text("SELECT set_config('app.tenant_id', :organization_id, true)"),
        {"organization_id": auth.active_organization_id},
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
    auth: AuthContext,
    wf_id: str,
) -> RelationshipBinding:
    return RelationshipBinding(
        subject=RelationshipSubject(
            type=RelationshipSubjectType(body.subject_type),
            id=body.subject_id,
            relation=body.subject_relation,
        ),
        relation=body.relation,
        resource=_workflow_resource(auth, wf_id),
    )


def _require_sharing_enabled() -> None:
    # Sharing stays undiscoverable until the enforcing backend and product
    if not config.resource_sharing_enabled:
        raise HTTPException(status_code=404, detail="resource_not_found")


@router.get("/{wf_id}/workspace")
async def get_workflow_workspace_identity(
    wf_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Return durable VFS identities without consulting or warming Sandbox."""
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.MOUNT,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    return {
        "workflow_scope_id": wf_id,
        "mount_scope_id": _mount_scope_id(auth.user_id),
    }


@router.get("", response_model=Page[WorkflowMetaOut])
async def list_workflows(
    request: Request,
    page: PageRequest = Depends(PageRequest.as_query),
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    principal = principal_for_auth(auth)
    context = context_for_auth(auth, request)
    authorized_ids = await service.list_authorized_ids(
        principal,
        Action.VIEW_METADATA,
        ResourceType.WORKFLOW,
        context,
    )
    rows, total = await repo.list_authorized_workflows(
        authorized_ids,
        limit=page.limit,
        offset=page.offset,
    )
    resources = [
        _workflow_resource(auth, item["wf_id"]) for item in rows
    ]
    decisions = await batch_resource_decisions(
        service,
        principal=principal,
        resources=resources,
        context=context,
    )
    provenance = ResourceProvenanceBuilder(session)
    items = [
        await _meta_to_out(item, decisions[resource], provenance)
        for item, resource in zip(rows, resources, strict=True)
    ]
    return Page[WorkflowMetaOut](
        items=items,
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("", response_model=WorkflowMetaOut, status_code=201)
async def create_workflow(
    body: WorkflowCreate,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    # Resource creation is an organization capability. Active guests and
    # auditors must not gain write access merely because RLS admits the row.
    await authorize_resource(
        request=request,
        auth=auth,
        service=service,
        resource=ResourceRef(
            ResourceType.ORGANIZATION,
            auth.active_organization_id,
            auth.active_organization_id,
        ),
        action=Action.CREATE,
    )
    meta = await repo.create_workflow(
        name=body.name, description=body.description, tags=body.tags,
    )
    coordinator = mutation_coordinator_for_request(
        request,
        auth.active_organization_id,
    )
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=frozenset(),
        after=resource_root_edges(
            organization_id=auth.active_organization_id,
            object_type="workflow",
            object_id=meta["wf_id"],
            owner_relation="manager",
            owner_type="user",
            owner_id=auth.user_id,
        ),
        operation_id=uuid.uuid4().hex,
        source="workflow-create",
    )
    # The row and its durable relationship intent become atomic before any
    # external OpenFGA call. Never return a resource whose relationship write
    # has not either applied or remained durably recoverable.
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_request_organization(session, auth)
    decision = await service.check(
        principal_for_auth(auth),
        Action.VIEW_METADATA,
        _workflow_resource(auth, meta["wf_id"]),
        context_for_auth(
            auth,
            request,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        ),
    )
    if not decision.allowed:
        raise OpenFgaUnavailableError(
            "authorization_projection_not_visible"
        )
    return await _meta_to_out(
        meta,
        decision,
        ResourceProvenanceBuilder(session),
    )


@router.get("/sandboxes")
async def get_workflow_sandbox_statuses(
    request: Request,
    wf_id: list[str] = Query(default=[]),
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Batch resident sandbox statuses for workflow management rows.

    Read-only and non-creating: the management page should show active resource
    placement without warming missing sandboxes.
    """
    items = []
    seen: set[str] = set()
    authorized_ids = set(await service.list_authorized_ids(
        principal_for_auth(auth),
        Action.INSPECT_RUNS,
        ResourceType.WORKFLOW,
        context_for_auth(auth, request),
    ))
    for workflow_id in wf_id[:200]:
        if (
            not workflow_id
            or workflow_id in seen
            or workflow_id not in authorized_ids
        ):
            continue
        seen.add(workflow_id)
        if not await repo.get_meta(workflow_id):
            continue
        items.append(await _workflow_sandbox_status_payload(
            session=session,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            wf_id=workflow_id,
        ))
    return {"items": items}


@router.get("/{wf_id}", response_model=WorkflowSnapshotOut)
async def get_workflow(
    wf_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.VIEW,
    )
    meta = await repo.get_meta(wf_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    wf = await repo.get_current_workflow(wf_id)
    return WorkflowSnapshotOut(
        workflow=wf,
        meta=await _meta_to_out(
            meta,
            authorized.decision,
            ResourceProvenanceBuilder(session),
        ),
    )


@router.get("/{wf_id}/sandbox")
async def get_workflow_sandbox_status(
    wf_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Resident sandbox status for a workflow edit surface.

    Read-only: Explorer can resolve the workflow's durable VFS roots without
    warming a sandbox process.
    """
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.INSPECT_RUNS,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    return await _workflow_sandbox_status_payload(
        session=session,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        wf_id=wf_id,
    )


@router.post("/{wf_id}/sandbox")
async def start_workflow_sandbox(
    wf_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Explicitly warm the workflow sandbox and the user's shared /mount."""
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.MOUNT,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    sandbox_session = await get_sandbox_manager().get_session(
        auth.tenant_id,
        wf_id,
        user_id=auth.user_id,
        expose_run=True,
    )
    await sandbox_session.prewarm_fileops()
    return await _workflow_sandbox_status_payload(
        session=session,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        wf_id=wf_id,
    )


@router.delete("/{wf_id}/sandbox")
async def close_workflow_sandbox(
    wf_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Release the resident workflow sandbox, if one is loaded."""
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.CANCEL,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    running_exec_ids = running_execution_ids(wf_id)
    for exec_id in running_exec_ids:
        stop_registry.signal(exec_id)
    await get_sandbox_manager().close_session(auth.tenant_id, wf_id)
    return await _workflow_sandbox_status_payload(
        session=session,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        wf_id=wf_id,
    )


@router.get("/{wf_id}/at/v{v}.sv{sv}", response_model=WorkflowSnapshotOut)
async def get_workflow_at(
    wf_id: str,
    v: int,
    sv: int,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.VIEW,
    )
    meta = await repo.get_meta(wf_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    wf = await repo.get_workflow_at(wf_id, v, sv)
    # `get_workflow_at` returns `{}` for BOTH "row missing" and "row present
    # but holds an empty workflow dict" (e.g. the seeded ``sv0`` init version,
    # or any genuinely-empty pinned snapshot). A bare ``if not wf`` 404s the
    # latter — which is exactly what the left-Explorer "load this version"
    # flow hits, surfacing as "Failed to load workflow." on the canvas. Gate
    # the 404 on real ROW existence instead, so an empty-but-present snapshot
    # loads (and the frontend renders the empty/onboarding canvas), while a
    # truly nonexistent pin still 404s.
    if not wf and not any(
        e["major"] == v and e["sub"] == sv
        for e in await repo.get_version_history(wf_id)
    ):
        raise HTTPException(status_code=404,
                            detail=f"snapshot v{v}.sv{sv} not found")
    return WorkflowSnapshotOut(
        workflow=wf,
        meta=await _meta_to_out(
            meta,
            authorized.decision,
            ResourceProvenanceBuilder(session),
        ),
    )


@router.patch("/{wf_id}", response_model=WorkflowMetaOut)
async def update_workflow_meta(
    wf_id: str,
    body: WorkflowMetaPatch,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    fields = body.model_dump(exclude_none=True)
    if "name" in fields:
        fields["workflow_name"] = fields.pop("name")
    # Re-check immediately before the durable mutation. The initial check
    # protects pre-mutation reads; this closes the revoke race.
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    meta = await repo.update_meta(wf_id, **fields)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    return await _meta_to_out(
        meta,
        authorized.decision,
        ResourceProvenanceBuilder(session),
    )


@router.delete("/{wf_id}", status_code=204)
async def delete_workflow(
    wf_id: str,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow(
        request=request,
        auth=ctx,
        service=service,
        wf_id=wf_id,
        action=Action.DELETE,
    )
    # Deployments T5 — Spec §10.4 app-layer guard. The deployments→workflows
    # FK is ``ON DELETE RESTRICT``, but workflows are soft-deleted (the
    # ``deleted_at`` column UPDATE bypasses RESTRICT). Block the delete
    # while an enabled, non-soft-deleted deployment still references this
    # workflow — otherwise an attacker / accidental delete leaves live
    # deployments pointing at a tombstoned workflow.
    #
    # RLS-scoped (tenant_db session) so this check naturally only sees the
    # caller's own deployments — a foreign-tenant deployment, even if it
    # referenced ``wf_id`` (it can't, the workflows FK is tenant-pinned),
    # would not register as in_use here, which is correct.
    in_use = (await session.execute(
        text(
            "SELECT 1 FROM deployments "
            "WHERE wf_id = :wf AND enabled = TRUE "
            "AND deleted_at IS NULL LIMIT 1"
        ),
        {"wf": wf_id},
    )).one_or_none()
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "workflow has enabled deployments — "
                "disable or delete them first"
            ),
        )
    # Capture the workflow name BEFORE the soft-delete for the audit snapshot.
    meta = await repo.get_meta(wf_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    workflow_name = meta.get("workflow_name")
    # Re-check at the transaction boundary before any destructive side effect.
    await _authorize_workflow(
        request=request,
        auth=ctx,
        service=service,
        wf_id=wf_id,
        action=Action.DELETE,
    )
    await get_sandbox_manager().close_session(ctx.tenant_id, wf_id)
    vfs_deleted = await VfsRepo(
        session,
        object_store=get_object_store(),
    ).delete_scope_prefixes(
        wf_id=wf_id,
        prefixes=["/data"],
    )
    run_deleted = await VfsRunRepo(
        session,
        get_object_store(),
        ctx.tenant_id,
    ).purge_workflow_runs(wf_id=wf_id)
    await repo.delete_workflow(wf_id)
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
            object_type="workflow",
            object_id=wf_id,
            owner_relation="manager",
            owner_type="user",
            owner_id=str(meta["owner_id"]),
        ),
        after=frozenset(),
        operation_id=uuid.uuid4().hex,
        source="workflow-delete",
    )
    await record_audit(
        session,
        action=actions.WORKFLOW_DELETE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=actions.TARGET_WORKFLOW,
        target_id=wf_id,
        target_name=workflow_name,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"vfs_deleted": vfs_deleted, "run_deleted": run_deleted},
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)


@router.get(
    "/{wf_id}/access",
    response_model=DirectBindingListOut,
)
async def list_workflow_access(
    wf_id: str,
    request: Request,
    continuation_token: str = "",
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> DirectBindingListOut:
    _require_sharing_enabled()
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.MANAGE_ACCESS,
    )
    try:
        page = await service.list_bindings(
            principal_for_auth(auth),
            authorized.resource,
            context_for_auth(
                auth,
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


async def _change_workflow_access(
    *,
    desired_present: bool,
    wf_id: str,
    body: DirectBindingIn,
    idempotency_key: str,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
) -> DirectBindingOut:
    _require_sharing_enabled()
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.MANAGE_ACCESS,
    )
    binding = (
        binding_from_share_resolution(
            body.resolution_token,
            relation=body.relation,
            actor_user_id=auth.user_id,
            session_id=auth.session_id,
            resource=_workflow_resource(auth, wf_id),
        )
        if desired_present and isinstance(body, DirectBindingGrantIn)
        else _binding_from_body(body, auth=auth, wf_id=wf_id)
    )
    try:
        result = await (
            service.grant(
                principal_for_auth(auth),
                binding,
                context_for_auth(
                    auth,
                    request,
                    consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
                ),
                idempotency_key=idempotency_key,
            )
            if desired_present
            else service.revoke(
                principal_for_auth(auth),
                binding,
                context_for_auth(
                    auth,
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
    "/{wf_id}/access",
    response_model=DirectBindingOut,
    status_code=status.HTTP_201_CREATED,
)
async def grant_workflow_access(
    wf_id: str,
    body: DirectBindingGrantIn,
    request: Request,
    idempotency_key: str = Header(
        min_length=1,
        max_length=200,
        alias="Idempotency-Key",
    ),
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    service: AuthzService = Depends(get_authz_service),
) -> DirectBindingOut:
    return await _change_workflow_access(
        desired_present=True,
        wf_id=wf_id,
        body=body,
        idempotency_key=idempotency_key,
        request=request,
        auth=auth,
        service=service,
    )


@router.delete(
    "/{wf_id}/access",
    response_model=DirectBindingOut,
)
async def revoke_workflow_access(
    wf_id: str,
    body: DirectBindingIn,
    request: Request,
    idempotency_key: str = Header(
        min_length=1,
        max_length=200,
        alias="Idempotency-Key",
    ),
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    service: AuthzService = Depends(get_authz_service),
) -> DirectBindingOut:
    return await _change_workflow_access(
        desired_present=False,
        wf_id=wf_id,
        body=body,
        idempotency_key=idempotency_key,
        request=request,
        auth=auth,
        service=service,
    )


@router.post("/{wf_id}/edits", response_model=EditsResponse)
async def apply_edits(
    wf_id: str,
    body: EditsRequest,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Apply vibe-ops incremental edits, then commit a new sv."""
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    # Serialize at the database through
    # SELECT ... FOR UPDATE on the Workflow row inside repo.commit
    # (the asyncio lock never covered the dependency-teardown commit).
    current = await repo.get_current_workflow(wf_id) or {}
    applied: list = []
    first_error: str | None = None
    first_error_index: int | None = None
    wf = current
    for i, op in enumerate(body.updates):
        new_wf, feedback = WorkflowUpdater.apply_updates(wf, [op])
        err_lines = [f for f in feedback if f.startswith("ERROR:")]
        if err_lines:
            first_error = "; ".join(err_lines)
            first_error_index = i
            break
        wf = new_wf
        applied.append(op)
    if applied:
        authorized = await _authorize_workflow(
            request=request,
            auth=auth,
            service=service,
            wf_id=wf_id,
            action=Action.UPDATE,
        )
        await repo.commit(
            wf_id, wf,
            note=f"edits +{len(applied)}/{len(body.updates)}",
        )
    else:
        authorized = await _authorize_workflow(
            request=request,
            auth=auth,
            service=service,
            wf_id=wf_id,
            action=Action.UPDATE,
        )
    meta = await repo.get_meta(wf_id)
    return EditsResponse(
        applied_count=len(applied),
        total_count=len(body.updates),
        new_meta=await _meta_to_out(
            meta,
            authorized.decision,
            ResourceProvenanceBuilder(session),
        ),
        first_error=first_error,
        first_error_index=first_error_index,
    )


@router.post("/{wf_id}/commits", response_model=WorkflowMetaOut)
async def commit_workflow(
    wf_id: str,
    body: CommitRequest,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Full-content commit (when the client wants to bypass incremental ops).

    UX-5: ``body.target_major`` (optional) lands the commit under a specific
    (historical) major instead of the active one — see ``repo.commit``. A
    stale / nonexistent major surfaces as 404.
    """
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    # DB row-lock (repo.commit FOR UPDATE) now serializes per-wf_id.
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    try:
        await repo.commit(
            wf_id, body.workflow, note=body.note,
            target_major=body.target_major,
        )
    except ValueError as e:
        # repo.commit raises ValueError when target_major doesn't exist.
        raise HTTPException(status_code=404, detail=str(e)) from e
    meta = await repo.get_meta(wf_id)
    return await _meta_to_out(
        meta,
        authorized.decision,
        ResourceProvenanceBuilder(session),
    )


@router.post("/{wf_id}/major-versions", response_model=WorkflowMetaOut)
async def new_major_version(
    wf_id: str,
    body: CommitRequest,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    # DB row-lock (repo.new_version FOR UPDATE) now serializes per-wf_id.
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    await repo.new_version(
        wf_id, body.workflow, note=body.note or "New Major Version",
    )
    meta = await repo.get_meta(wf_id)
    return await _meta_to_out(
        meta,
        authorized.decision,
        ResourceProvenanceBuilder(session),
    )


@router.get("/{wf_id}/versions")
async def list_versions(
    wf_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.VIEW,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    return {"versions": await repo.get_version_history(wf_id)}


@router.post("/{wf_id}/undo", response_model=WorkflowSnapshotOut)
async def undo_workflow(
    wf_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    meta = await repo.get_meta(wf_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    cur_sv = meta.get("active_sv", 0)
    if cur_sv <= 0:
        raise HTTPException(status_code=409, detail="already at sv=0")
    # DB row-lock (repo.set_head FOR UPDATE) now serializes per-wf_id.
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    new_meta = await repo.set_head(
        wf_id, meta.get("active_v", 1), cur_sv - 1)
    wf = await repo.get_current_workflow(wf_id)
    return WorkflowSnapshotOut(
        workflow=wf,
        meta=await _meta_to_out(
            new_meta,
            authorized.decision,
            ResourceProvenanceBuilder(session),
        ),
    )


@router.post("/{wf_id}/redo", response_model=WorkflowSnapshotOut)
async def redo_workflow(
    wf_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    meta = await repo.get_meta(wf_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    v = meta.get("active_v", 1)
    cur_sv = meta.get("active_sv", 0)
    max_sv = await repo.max_subversion(wf_id, v)
    if cur_sv >= max_sv:
        raise HTTPException(status_code=409, detail="already at latest sv")
    # DB row-lock (repo.set_head FOR UPDATE) now serializes per-wf_id.
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    new_meta = await repo.set_head(wf_id, v, cur_sv + 1)
    wf = await repo.get_current_workflow(wf_id)
    return WorkflowSnapshotOut(
        workflow=wf,
        meta=await _meta_to_out(
            new_meta,
            authorized.decision,
            ResourceProvenanceBuilder(session),
        ),
    )


@router.post("/{wf_id}/checkout", response_model=WorkflowSnapshotOut)
async def checkout_version(
    wf_id: str,
    body: CheckoutRequest,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    meta = await repo.get_meta(wf_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    wf = await repo.get_workflow_at(wf_id, body.v, body.sv)
    if not wf:
        raise HTTPException(
            status_code=404,
            detail=f"snapshot v{body.v}.sv{body.sv} not found",
        )
    # DB row-lock (repo.set_head FOR UPDATE) now serializes per-wf_id.
    authorized = await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    new_meta = await repo.set_head(wf_id, body.v, body.sv)
    return WorkflowSnapshotOut(
        workflow=wf,
        meta=await _meta_to_out(
            new_meta,
            authorized.decision,
            ResourceProvenanceBuilder(session),
        ),
    )


@router.post("/{wf_id}/check", response_model=CheckResponse)
async def check_workflow(
    wf_id: str,
    request: Request,
    body: CheckRequest | None = None,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.VIEW,
    )
    from ..services.platform_mcp.config_tools import workflow_model_catalog_for_user

    model_catalog = await workflow_model_catalog_for_user(session, auth.user_id)
    return await _check_workflow_content(
        wf_id,
        body=body,
        repo=repo,
        available_model_ids=set(model_catalog),
    )


async def _check_workflow_content(
    wf_id: str,
    *,
    body: CheckRequest | None,
    repo: WorkflowRepo,
    available_model_ids: set[str] | None = None,
) -> CheckResponse:
    """Pure workflow validation after the HTTP authorization boundary."""
    from vibecanvas_engine import Workflow

    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    # Validate the in-progress DRAFT when the client sends one (so the user can
    # Check unsaved edits WITHOUT saving first); otherwise fall back to the
    # committed current version.
    if body is not None and body.workflow is not None:
        wf = body.workflow
    else:
        wf = await repo.get_current_workflow(wf_id) or {}
    result = Workflow.check(wf)
    if available_model_ids is not None:
        from ..services.platform_mcp.build_tools.workflow_file import (
            validate_workflow_model_names,
        )

        model_errors = validate_workflow_model_names(wf, available_model_ids)
        if model_errors:
            model_message = "\n".join(
                f"[{error['node_id']}] {error['message']}"
                for error in model_errors
            )
            existing = str(result.get("error_message") or "").strip()
            result = {
                **result,
                "status": "error",
                "error_message": "\n".join(
                    part for part in (existing, model_message) if part
                ),
            }
    return CheckResponse(**result)


@router.get("/{wf_id}/nodes/{node_id}/prompt-history",
            response_model=PromptHistoryOut)
async def get_prompt_history(
    wf_id: str,
    node_id: str,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.VIEW,
    )
    meta = await repo.get_meta(wf_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    cur = await repo.get_current_workflow(wf_id) or {}
    current_prompt = (
        cur.get(node_id, {}).get("node_config", {}).get("prompt_template", "")
    )
    v = meta.get("active_v", 1)
    subs = await repo.list_subversions(wf_id, v)
    history: list[str] = []
    seen: set[str] = set()
    for sv in reversed(subs):
        if len(history) >= 5:
            break
        wf = await repo.get_workflow_at(wf_id, v, sv)
        if not wf:
            continue
        p = wf.get(node_id, {}).get("node_config", {}).get("prompt_template", "")
        if p and p != current_prompt and p not in seen:
            history.append(p)
            seen.add(p)
    history.reverse()
    return PromptHistoryOut(node_id=node_id, prompts=history, current=current_prompt)


# ---------------------------------------------------------------------------
# Atomic batch submission.
#
# ``tasks.id == tasks.background_job_id == response.task_id``. The DB row is
# the durable audit (RLS-scoped to the caller's tenant); enqueue is reconciled
# every 30s by ``background.reconcile_queued``.
# ---------------------------------------------------------------------------


class BatchSubmitBody(BaseModel):
    """Atomic-submit body. ``extra='ignore'`` so a client cannot smuggle
    ``tenant_id`` / ``user_id`` / ``background_job_id`` into the row — those are
    derived from the authenticated context, never from the request."""

    model_config = ConfigDict(extra="ignore")

    data_source: dict
    column_mapping: dict
    # Optional output destination for the aggregated results table. v1 shape:
    # {"type": "vfs_data", "path": "/data/results.csv"}. None → results stay only
    # in the task's downloadable object-store copy (legacy behavior).
    output: dict | None = None
    # Optional user-defined output-column schema. When given, the results table
    # (object-store copy + destination) is EXACTLY these columns in order. Each
    # item: {"kind": "index"|"status"|"execution_time"|"error"|"field", "name": <header>,
    # and for "field": "node", "field", optional "default"}. Lenient: malformed
    # column dicts degrade to empty cells (NOT 422); None → legacy fixed columns.
    output_columns: list | None = None
    # How many rows run in parallel (thread pool). Clamped 1..16 in the task.
    concurrency: int = 1


@router.post("/{wf_id}/batch", status_code=status.HTTP_201_CREATED)
async def submit_batch(
    wf_id: str,
    body: BatchSubmitBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Submit a batch atomically.

    Inserts a ``tasks`` row inside the request transaction, then
    enqueues a durable workflow with ``workflow_id == tasks.id`` (so DBOS and
    the business row share one idempotency key used by the reconciler).
    """
    await _authorize_workflow(
        request=request,
        auth=ctx,
        service=service,
        wf_id=wf_id,
        action=Action.EXECUTE,
    )
    # Validate the output destination up front (cheap, no I/O) so a bad path /
    # unsupported type is a 422 at submit, not a runtime task failure.
    if body.output is not None:
        try:
            build_output_sink(
                body.output, wf_id=wf_id, tenant_id=ctx.tenant_id,
                default_name="results.csv",
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(e),
            ) from e

    task_id = uuid.uuid4()
    service_account_id = uuid.uuid4()
    await ServiceAccountsRepo(session).create_for_owner(
        service_account_id=service_account_id,
        tenant_id=uuid.UUID(ctx.tenant_id),
        name=f"Batch task: {wf_id}",
        kind="task",
        owner_resource_type="task",
        owner_resource_id=str(task_id),
        created_by=uuid.UUID(ctx.user_id),
    )
    credential_ids = await bind_workflow_credentials(
        session,
        tenant_id=uuid.UUID(ctx.tenant_id),
        service_account_id=service_account_id,
        created_by=ctx.user_id,
        workflow=await WorkflowRepo(
            session, ctx.user_id
        ).get_current_workflow(wf_id),
    )
    await TasksRepo(session).create(
        task_id=task_id,
        tenant_id=uuid.UUID(ctx.tenant_id),
        user_id=uuid.UUID(ctx.user_id),
        workflow_id=wf_id,
        task_type="batch_exec",
        payload=body.model_dump(),
        background_job_id=str(task_id),
        service_account_id=service_account_id,
    )
    # Close the permission-revocation race immediately before the durable
    # queued Task row is introduced.
    await _authorize_workflow(
        request=request,
        auth=ctx,
        service=service,
        wf_id=wf_id,
        action=Action.EXECUTE,
    )
    try:
        await session.flush()
    except IntegrityError as e:
        # workflow_id is an FK to workflows(wf_id); a missing/foreign-tenant
        # workflow surfaces here as a constraint violation.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"workflow {wf_id} not found",
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
        after=(
            resource_root_edges(
                organization_id=ctx.active_organization_id,
                object_type="task",
                object_id=str(task_id),
                owner_relation="manager",
                owner_type="user",
                owner_id=ctx.user_id,
            )
            | service_account_edges(
                organization_id=ctx.active_organization_id,
                service_account_id=str(service_account_id),
                created_by=ctx.user_id,
                owner_resource_type="task",
                owner_resource_id=str(task_id),
                workflow_id=wf_id,
                credential_ids=credential_ids,
            )
        ),
        operation_id=uuid.uuid4().hex,
        source="batch-task-create",
    )
    # The Task row and authorization intent must become durable before the
    # external queue observes the task id.
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_request_organization(session, ctx)

    # A failure here is safe: the business row is already durably queued and
    # the periodic reconciler will idempotently enqueue it again.
    try:
        await enqueue_background_job_async(
            "batch_exec",
            job_id=str(task_id),
            queue="interactive",
            kwargs={"task_id": str(task_id)},
        )
    except Exception:
        # Swallow — the row is durably queued and the reconciler owns
        # delivery reliability. Re-raising would leave the row in
        # ``queued`` AND return 5xx to the client (worst of both).
        pass

    return {"task_id": str(task_id)}
