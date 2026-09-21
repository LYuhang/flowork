"""Task Center routes.

Task Center tracks user-managed ``batch_exec`` and ``scheduled_run`` work.
Online API/webhook deployment invocations and KB indexing do not create
Task rows. ``task_events.event_type`` uses the unified protocol:
``state | progress | log | result | terminal``; concrete actions and
status snapshots live in the JSON payload.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
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
    service_account_edges,
)
from vibecanvas_api.authorization.service import (
    AuthorizationDeniedError,
    AuthzService,
    batch_resource_decisions,
)
from vibecanvas_api.authorization.stream_guard import (
    authorization_lease_is_valid,
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
from vibecanvas_api.config import config as _config
from vibecanvas_api.schemas.access import (
    DirectBindingGrantIn,
    DirectBindingIn,
    DirectBindingListOut,
    DirectBindingOut,
    access_from_decision,
    decision_allows_content,
)
from vibecanvas_api.services.background_queue import (
    cancel_background_job_async,
    enqueue_background_job_in_transaction,
)
from vibecanvas_api.services.batch_output import serialize_results
from vibecanvas_api.services.access_presentation import direct_binding_out
from vibecanvas_api.services.object_store import get_object_store, uri_to_key
from vibecanvas_api.services.queue_routing import route_for
from vibecanvas_api.services.resource_provenance import (
    ResourceProvenanceBuilder,
)
from vibecanvas_api.services.scheduled_runs import (
    DEFAULT_NOTIFICATION_POLICY,
    compute_next_run_at,
    execution_to_out,
    merge_notification_policy,
    schedule_to_out,
)
from vibecanvas_api.services.service_account_credentials import (
    bind_workflow_credentials,
)
from vibecanvas_api.services.sse_bridge import task_event_stream
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _next_object_chunk(chunks: Iterator[bytes]) -> bytes | None:
    return next(chunks, None)


def _stream_object_chunks(
    first: bytes | None,
    remaining: Iterator[bytes],
) -> Iterator[bytes]:
    if first is not None:
        yield first
    yield from remaining


class CancelBody(BaseModel):
    """Cancel request body. ``extra='ignore'`` so unknown fields are
    dropped silently (defence in depth — the only knob we honour is
    ``mode``)."""

    model_config = ConfigDict(extra="ignore")
    mode: str = "soft"   # "soft" | "force"


class ScheduledRunCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    workflow_id: str
    enabled: bool = True
    schedule_type: str = "interval"
    interval_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str = "UTC"
    start_at: datetime | None = None
    end_at: datetime | None = None
    input_preset: dict = Field(default_factory=dict)
    mount_enabled: bool = False
    notification_policy: dict = Field(default_factory=lambda: dict(DEFAULT_NOTIFICATION_POLICY))


class ScheduledRunPatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    enabled: bool | None = None
    schedule_type: str | None = None
    interval_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    input_preset: dict | None = None
    mount_enabled: bool | None = None
    notification_policy: dict | None = None


# Status sets — keep at module scope so they're cheap to import-time
# verify against the ``ck_tasks_status`` CHECK constraint in
# ``models_tasks.py``.
_TERMINAL_OR_INFLIGHT_CANCEL = (
    "finished",
    "finished_with_errors",
    "failed",
    "interrupted",
    "cancelling",
    "cancelled",
)
_RESUMABLE_BATCH_STATUSES = (
    "cancelled",
    "failed",
    "interrupted",
    "finished_with_errors",
)


async def _task_to_out(
    t,
    decision: Decision,
    provenance: ResourceProvenanceBuilder,
) -> dict:
    """Serialize a ``Task`` ORM row to the JSON contract.

    Datetime columns become ISO-8601 strings; ``None`` stays ``None``.
    UUIDs are stringified so the JSON contract is portable (the
    frontend treats task ids as opaque strings).
    """
    can_view_content = decision_allows_content(decision)
    return {
        "id": str(t.id),
        "status": t.status,
        "progress": t.progress,
        "task_type": t.task_type,
        "workflow_id": t.workflow_id,
        "payload": t.payload if can_view_content else {},
        "result": t.result if can_view_content else None,
        "results_uri": t.results_uri if can_view_content else None,
        "error": t.error if can_view_content else None,
        "background_job_id": t.background_job_id if can_view_content else None,
        "sandbox_status": (
            (t.payload or {}).get("sandbox_status")
            if can_view_content else None
        ),
        "submitted_at": t.submitted_at.isoformat() if t.submitted_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        "access": access_from_decision(decision).model_dump(mode="json"),
        "provenance": (
            await provenance.build(creator_user_id=t.user_id)
        ).model_dump(mode="json"),
    }


def _event_to_out(ev) -> dict:
    return {
        "id": ev.id,
        "task_id": str(ev.task_id),
        "ts": ev.ts.isoformat() if ev.ts else None,
        "event_type": ev.event_type,
        "payload": ev.payload,
    }


def _schedule_task_payload(schedule, next_run_at=None) -> dict:
    return {
        "name": schedule.name,
        "schedule_id": str(schedule.id),
        "schedule_type": schedule.schedule_type,
        "cron_expr": schedule.cron_expr,
        "interval_seconds": schedule.interval_seconds,
        "timezone": schedule.timezone,
        "next_run_at": (
            next_run_at.isoformat()
            if next_run_at is not None
            else schedule.next_run_at.isoformat() if schedule.next_run_at else None
        ),
        "end_at": schedule.end_at.isoformat() if getattr(schedule, "end_at", None) else None,
        "last_status": schedule.last_status,
        "notification_policy": schedule.notification_policy,
    }


def _task_resource(ctx: AuthContext, task_id: uuid.UUID | str) -> ResourceRef:
    return ResourceRef(
        ResourceType.TASK,
        str(task_id),
        ctx.active_organization_id,
    )


async def _authorize_task(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    task_id: uuid.UUID | str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = _task_resource(ctx, task_id)
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


async def _authorize_workflow_use(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    workflow_id: str,
) -> None:
    decision = await service.check(
        principal_for_auth(ctx),
        Action.USE,
        ResourceRef(
            ResourceType.WORKFLOW,
            workflow_id,
            ctx.active_organization_id,
        ),
        context_for_auth(ctx, request),
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
    task_id: uuid.UUID,
) -> RelationshipBinding:
    return RelationshipBinding(
        subject=RelationshipSubject(
            type=RelationshipSubjectType(body.subject_type),
            id=body.subject_id,
            relation=body.subject_relation,
        ),
        relation=body.relation,
        resource=_task_resource(ctx, task_id),
    )


def _require_sharing_enabled() -> None:
    if not _config.resource_sharing_enabled:
        raise HTTPException(status_code=404, detail="resource_not_found")


async def _send_scheduled_execution(
    *,
    session: AsyncSession,
    task_id: uuid.UUID,
    schedule_id: uuid.UUID,
    execution_id: uuid.UUID,
    tenant_id: str,
    user_id: str,
    workflow_id: str,
) -> None:
    await enqueue_background_job_in_transaction(
        session,
        "scheduled_runs.execute",
        job_id=str(execution_id),
        queue=route_for("scheduled_run"),
        kwargs={"execution_id": str(execution_id)},
    )


@router.get("")
async def list_tasks(
    request: Request,
    status: list[str] = Query(default=[]),
    task_type: list[str] = Query(default=[]),
    workflow_id: str | None = None,
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Paginated, RLS-scoped task listing.

    Filters compose with AND:
      * ``status`` — multi-value (``?status=queued&status=running``).
      * ``task_type`` — multi-value.
      * ``workflow_id`` — single value (or omitted to mean ``any``).

    Ordering is newest-first by ``submitted_at`` (see
    ``TasksRepo.list_for_tenant``). Empty filter lists collapse to
    ``None`` so the SQL stays index-friendly.
    """
    principal = principal_for_auth(ctx)
    context = context_for_auth(ctx, request)
    authorized_ids = await service.list_authorized_ids(
        principal,
        Action.VIEW_METADATA,
        ResourceType.TASK,
        context,
    )
    items, total = await TasksRepo(session).list_for_tenant(
        task_ids=authorized_ids,
        status=status or None,
        task_type=task_type or None,
        workflow_id=workflow_id,
        search=q,
        limit=limit,
        offset=offset,
    )
    resources = [_task_resource(ctx, item.id) for item in items]
    decisions = await batch_resource_decisions(
        service,
        principal=principal,
        resources=resources,
        context=context,
    )
    provenance = ResourceProvenanceBuilder(session)
    output_items = [
        await _task_to_out(item, decisions[resource], provenance)
        for item, resource in zip(items, resources, strict=True)
    ]
    return {
        "items": output_items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/summary")
async def tasks_summary(
    request: Request,
    task_type: list[str] | None = Query(default=None),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    authorized_ids = await service.list_authorized_ids(
        principal_for_auth(ctx),
        Action.VIEW_METADATA,
        ResourceType.TASK,
        context_for_auth(ctx, request),
    )
    return await TasksRepo(session).summary_for_tenant(
        task_ids=authorized_ids,
        task_type=task_type,
    )


@router.post("/scheduled-runs", status_code=status.HTTP_201_CREATED)
async def create_scheduled_run(
    body: ScheduledRunCreateBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    await _authorize_workflow_use(
        request=request,
        ctx=ctx,
        service=service,
        workflow_id=body.workflow_id,
    )
    if body.schedule_type not in {"interval", "cron"}:
        raise HTTPException(status_code=422, detail="schedule_type must be interval or cron")
    if body.schedule_type == "interval" and (body.interval_seconds or 0) <= 0:
        raise HTTPException(status_code=422, detail="interval_seconds must be positive")
    if body.schedule_type == "cron" and not body.cron_expr:
        raise HTTPException(status_code=422, detail="cron_expr is required")
    meta = await WorkflowRepo(session, ctx.user_id).get_meta(body.workflow_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"workflow {body.workflow_id} not found")
    try:
        next_run_at = (
            compute_next_run_at(
                schedule_type=body.schedule_type,
                timezone_name=body.timezone,
                interval_seconds=body.interval_seconds,
                cron_expr=body.cron_expr,
                start_at=body.start_at,
            )
            if body.enabled
            else None
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    normalized_end_at = (
        body.end_at.astimezone(timezone.utc)
        if body.end_at is not None and body.end_at.tzinfo is not None
        else body.end_at.replace(tzinfo=timezone.utc)
        if body.end_at is not None
        else None
    )
    if normalized_end_at is not None and next_run_at is not None and normalized_end_at < next_run_at:
        raise HTTPException(
            status_code=422,
            detail="end_at must not be earlier than the first scheduled run",
        )
    task_id = uuid.uuid4()
    schedule_id = uuid.uuid4()
    service_account_id = uuid.uuid4()
    repo = TasksRepo(session)
    try:
        await ServiceAccountsRepo(session).create_for_owner(
            service_account_id=service_account_id,
            tenant_id=uuid.UUID(ctx.tenant_id),
            name=f"Schedule: {body.name.strip() or 'Scheduled run'}",
            kind="schedule",
            owner_resource_type="task",
            owner_resource_id=str(task_id),
            created_by=uuid.UUID(ctx.user_id),
            status="active" if body.enabled else "disabled",
        )
        credential_ids = await bind_workflow_credentials(
            session,
            tenant_id=uuid.UUID(ctx.tenant_id),
            service_account_id=service_account_id,
            created_by=ctx.user_id,
            workflow=await WorkflowRepo(
                session, ctx.user_id
            ).get_current_workflow(body.workflow_id),
        )
        task, schedule = await repo.create_schedule(
            task_id=task_id,
            schedule_id=schedule_id,
            tenant_id=uuid.UUID(ctx.tenant_id),
            user_id=uuid.UUID(ctx.user_id),
            workflow_id=body.workflow_id,
            name=body.name.strip() or "Scheduled run",
            enabled=body.enabled,
            schedule_type=body.schedule_type,
            cron_expr=body.cron_expr,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone or "UTC",
            input_preset=body.input_preset or {},
            mount_enabled=body.mount_enabled,
            notification_policy=merge_notification_policy(body.notification_policy),
            next_run_at=next_run_at,
            end_at=normalized_end_at,
            service_account_id=service_account_id,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=404, detail=f"workflow {body.workflow_id} not found") from exc
    await repo.insert_event(
        task_id,
        "state",
        {
            "schema_version": 1,
            "level": "info",
            "category": "scheduled_run",
            "action": "scheduled_run.created",
            "message": "Scheduled run created.",
            "task_status": task.status,
            "sandbox_status": "released",
            "scope": {"type": "task", "id": str(task_id), "name": schedule.name},
            "progress": None,
            "data": {"schedule_id": str(schedule_id), "next_run_at": _schedule_task_payload(schedule)["next_run_at"]},
            "error": None,
        },
        uuid.UUID(ctx.tenant_id),
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
                workflow_id=body.workflow_id,
                credential_ids=credential_ids,
            )
        ),
        operation_id=uuid.uuid4().hex,
        source="scheduled-task-create",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_request_organization(session, ctx)
    decision = await service.check(
        principal_for_auth(ctx),
        Action.VIEW_METADATA,
        _task_resource(ctx, task_id),
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
    return {
        "task": await _task_to_out(
            task,
            decision,
            ResourceProvenanceBuilder(session),
        ),
        "schedule": schedule_to_out(schedule),
    }


@router.get("/scheduled-runs/{task_id}")
async def get_scheduled_run(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    authorized = await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.VIEW,
    )
    repo = TasksRepo(session)
    task = await repo.get(task_id)
    schedule = await repo.get_schedule_by_task(task_id)
    if task is None or schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    return {
        "task": await _task_to_out(
            task,
            authorized.decision,
            ResourceProvenanceBuilder(session),
        ),
        "schedule": schedule_to_out(schedule),
    }


@router.patch("/scheduled-runs/{task_id}")
async def update_scheduled_run(
    task_id: uuid.UUID,
    body: ScheduledRunPatchBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.UPDATE,
    )
    repo = TasksRepo(session)
    task = await repo.get(task_id)
    schedule = await repo.get_schedule_by_task(task_id)
    if task is None or schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    fields = {}
    for key in ("name", "schedule_type", "interval_seconds", "cron_expr", "timezone", "input_preset", "mount_enabled", "end_at"):
        value = getattr(body, key)
        if value is not None:
            fields[key] = value
    if body.notification_policy is not None:
        fields["notification_policy"] = merge_notification_policy(body.notification_policy)
    enabled = schedule.enabled if body.enabled is None else body.enabled
    schedule_type = fields.get("schedule_type", schedule.schedule_type)
    interval_seconds = fields.get("interval_seconds", schedule.interval_seconds)
    cron_expr = fields.get("cron_expr", schedule.cron_expr)
    timezone_name = fields.get("timezone", schedule.timezone)
    try:
        next_run_at = (
            compute_next_run_at(
                schedule_type=schedule_type,
                timezone_name=timezone_name,
                interval_seconds=interval_seconds,
                cron_expr=cron_expr,
                start_at=body.start_at,
            )
            if enabled
            else None
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    fields["enabled"] = enabled
    fields["next_run_at"] = next_run_at
    authorized = await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    schedule = await repo.update_schedule(schedule.id, **fields)
    if task.service_account_id is not None:
        await ServiceAccountsRepo(session).set_status(
            task.service_account_id,
            status="active" if enabled else "disabled",
        )
    await repo.update_status(
        task_id,
        status="enabled" if enabled else "paused",
        payload=_schedule_task_payload(schedule),
    )
    updated_task = await repo.get(task_id)
    assert updated_task is not None
    return {
        "task": await _task_to_out(
            updated_task,
            authorized.decision,
            ResourceProvenanceBuilder(session),
        ),
        "schedule": schedule_to_out(schedule),
    }


@router.post("/scheduled-runs/{task_id}/pause")
async def pause_scheduled_run(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.UPDATE,
    )
    repo = TasksRepo(session)
    schedule = await repo.get_schedule_by_task(task_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    schedule = await repo.update_schedule(schedule.id, enabled=False, next_run_at=None)
    task = await repo.get(task_id)
    if task is not None and task.service_account_id is not None:
        await ServiceAccountsRepo(session).set_status(
            task.service_account_id,
            status="disabled",
        )
    await repo.update_status(task_id, status="paused", payload=_schedule_task_payload(schedule))
    return {"status": "paused", "schedule": schedule_to_out(schedule)}


@router.post("/scheduled-runs/{task_id}/resume")
async def resume_scheduled_run(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.RESUME,
    )
    repo = TasksRepo(session)
    schedule = await repo.get_schedule_by_task(task_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    try:
        next_run_at = compute_next_run_at(
            schedule_type=schedule.schedule_type,
            timezone_name=schedule.timezone,
            interval_seconds=schedule.interval_seconds,
            cron_expr=schedule.cron_expr,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.RESUME,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    schedule = await repo.update_schedule(schedule.id, enabled=True, next_run_at=next_run_at)
    task = await repo.get(task_id)
    if task is not None and task.service_account_id is not None:
        await ServiceAccountsRepo(session).set_status(
            task.service_account_id,
            status="active",
        )
    await repo.update_status(task_id, status="enabled", payload=_schedule_task_payload(schedule))
    return {"status": "enabled", "schedule": schedule_to_out(schedule)}


@router.post("/scheduled-runs/{task_id}/run-now", status_code=status.HTTP_202_ACCEPTED)
async def run_scheduled_now(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.EXECUTE,
    )
    repo = TasksRepo(session)
    task = await repo.get(task_id)
    schedule = await repo.get_schedule_by_task(task_id)
    if task is None or schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    active, _ = await repo.list_scheduled_executions(schedule_id=schedule.id, limit=10)
    if any(ex.status in {"queued", "running"} for ex in active):
        raise HTTPException(status_code=409, detail="a scheduled execution is already active")
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.EXECUTE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    execution_id = uuid.uuid4()
    run_key = f"manual:{execution_id}"
    execution = await repo.create_scheduled_execution(
        execution_id=execution_id,
        tenant_id=uuid.UUID(ctx.tenant_id),
        schedule_id=schedule.id,
        workflow_id=schedule.workflow_id,
        run_key=run_key,
        trigger_type="manual",
        input_snapshot=schedule.input_preset or {},
    )
    if execution is None:
        raise HTTPException(status_code=409, detail="duplicate scheduled execution")
    await repo.insert_event(
        task_id,
        "state",
        {
            "schema_version": 1,
            "level": "info",
            "category": "scheduled_run",
            "action": "scheduled_run.manual_queued",
            "message": "Manual scheduled run queued.",
            "task_status": "running",
            "sandbox_status": "pending",
            "scope": {"type": "scheduled_run_execution", "id": str(execution_id), "name": None},
            "progress": None,
            "data": {"execution_id": str(execution_id), "schedule_id": str(schedule.id)},
            "error": None,
        },
        uuid.UUID(ctx.tenant_id),
    )
    await repo.update_status(task_id, status="running", background_job_id=str(execution_id))
    await session.flush()
    await _send_scheduled_execution(
        session=session,
        task_id=task_id,
        schedule_id=schedule.id,
        execution_id=execution_id,
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        workflow_id=schedule.workflow_id,
    )
    return {"status": "queued", "execution": execution_to_out(execution)}


@router.delete("/scheduled-runs/{task_id}")
async def delete_scheduled_run(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.DELETE,
    )
    repo = TasksRepo(session)
    task = await repo.get(task_id)
    schedule = await repo.get_schedule_by_task(task_id)
    if task is None or schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    active, _ = await repo.list_scheduled_executions(schedule_id=schedule.id, limit=10)
    if any(ex.status in {"queued", "running"} for ex in active):
        raise HTTPException(status_code=409, detail="cancel active execution before deleting schedule")
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    account_before = frozenset()
    if task.service_account_id is not None:
        account_repo = ServiceAccountsRepo(session)
        credential_ids = tuple(
            str(value)
            for value in await account_repo.credential_ids(
                task.service_account_id
            )
        )
        account_before = service_account_edges(
            organization_id=ctx.active_organization_id,
            service_account_id=str(task.service_account_id),
            created_by=str(task.user_id),
            owner_resource_type="task",
            owner_resource_id=str(task_id),
            workflow_id=str(task.workflow_id),
            credential_ids=credential_ids,
        )
        await account_repo.set_status(
            task.service_account_id,
            status="deleted",
        )
    await session.execute(text("DELETE FROM tasks WHERE id=:id"), {"id": task_id})
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
                object_type="task",
                object_id=str(task_id),
                owner_relation="manager",
                owner_type="user",
                owner_id=str(task.owner_id),
            )
            | account_before
        ),
        after=frozenset(),
        operation_id=uuid.uuid4().hex,
        source="scheduled-task-delete",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return {"status": "deleted"}


@router.get("/scheduled-runs/{task_id}/executions")
async def list_scheduled_run_executions(
    task_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.INSPECT_RUNS,
    )
    repo = TasksRepo(session)
    schedule = await repo.get_schedule_by_task(task_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    items, total = await repo.list_scheduled_executions(
        schedule_id=schedule.id,
        limit=limit,
        offset=offset,
    )
    return {"items": [execution_to_out(x) for x in items], "total": total, "limit": limit, "offset": offset}


@router.get("/scheduled-runs/{task_id}/executions/{execution_id}")
async def get_scheduled_run_execution(
    task_id: uuid.UUID,
    execution_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.INSPECT_RUNS,
    )
    repo = TasksRepo(session)
    schedule = await repo.get_schedule_by_task(task_id)
    execution = await repo.get_scheduled_execution(execution_id)
    if schedule is None or execution is None or execution.schedule_id != schedule.id:
        raise HTTPException(status_code=404, detail=f"execution {execution_id} not found")
    return execution_to_out(execution)


@router.get("/scheduled-runs/{task_id}/executions/{execution_id}/events")
async def get_scheduled_run_execution_events(
    task_id: uuid.UUID,
    execution_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=500, ge=1, le=1000),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.INSPECT_RUNS,
    )
    repo = TasksRepo(session)
    schedule = await repo.get_schedule_by_task(task_id)
    execution = await repo.get_scheduled_execution(execution_id)
    if schedule is None or execution is None or execution.schedule_id != schedule.id:
        raise HTTPException(status_code=404, detail=f"execution {execution_id} not found")
    events = await repo.events_for_task(task_id=task_id, limit=limit)
    filtered = []
    for event in events:
        payload = event.payload or {}
        data = payload.get("data") if isinstance(payload, dict) else None
        scope = payload.get("scope") if isinstance(payload, dict) else None
        if (
            isinstance(data, dict)
            and data.get("execution_id") == str(execution_id)
        ) or (
            isinstance(scope, dict)
            and scope.get("id") == str(execution_id)
        ):
            filtered.append(event)
    return {"items": [_event_to_out(x) for x in filtered], "limit": limit}


@router.post("/scheduled-runs/{task_id}/executions/{execution_id}/cancel")
async def cancel_scheduled_run_execution(
    task_id: uuid.UUID,
    execution_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.CANCEL,
    )
    repo = TasksRepo(session)
    schedule = await repo.get_schedule_by_task(task_id)
    execution = await repo.get_scheduled_execution(execution_id)
    if schedule is None or execution is None or execution.schedule_id != schedule.id:
        raise HTTPException(status_code=404, detail=f"execution {execution_id} not found")
    if execution.status not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail=f"execution is {execution.status}, cannot cancel")
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.CANCEL,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    cancelled_at = datetime.now(timezone.utc)
    await repo.update_scheduled_execution(
        execution_id,
        status="cancelled",
        finished_at=cancelled_at,
        error="Cancelled by user.",
    )
    await repo.update_schedule(
        schedule.id,
        last_run_at=cancelled_at,
        last_status="cancelled",
    )
    await repo.insert_event(
        task_id,
        "terminal",
        {
            "schema_version": 1,
            "level": "warning",
            "category": "scheduled_run",
            "action": "scheduled_run.cancelled",
            "message": "Scheduled execution cancellation requested.",
            "task_status": "enabled" if schedule.enabled else "paused",
            "sandbox_status": "releasing",
            "scope": {"type": "scheduled_run_execution", "id": str(execution_id), "name": None},
            "progress": None,
            "data": {"execution_id": str(execution_id), "schedule_id": str(schedule.id)},
            "error": None,
        },
        uuid.UUID(ctx.tenant_id),
    )
    await repo.update_status(task_id, status="enabled" if schedule.enabled else "paused")
    return {"status": "cancelled"}


@router.get(
    "/{task_id}/access",
    response_model=DirectBindingListOut,
)
async def list_task_access(
    task_id: uuid.UUID,
    request: Request,
    continuation_token: str = "",
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> DirectBindingListOut:
    _require_sharing_enabled()
    authorized = await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
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


async def _change_task_access(
    *,
    desired_present: bool,
    task_id: uuid.UUID,
    body: DirectBindingIn,
    idempotency_key: str,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
) -> DirectBindingOut:
    _require_sharing_enabled()
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.MANAGE_ACCESS,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    binding = (
        binding_from_share_resolution(
            body.resolution_token,
            relation=body.relation,
            actor_user_id=ctx.user_id,
            session_id=ctx.session_id,
            resource=_task_resource(ctx, task_id),
        )
        if desired_present and isinstance(body, DirectBindingGrantIn)
        else _binding_from_body(body, ctx=ctx, task_id=task_id)
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
    "/{task_id}/access",
    response_model=DirectBindingOut,
    status_code=status.HTTP_201_CREATED,
)
async def grant_task_access(
    task_id: uuid.UUID,
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
    return await _change_task_access(
        desired_present=True,
        task_id=task_id,
        body=body,
        idempotency_key=idempotency_key,
        request=request,
        ctx=ctx,
        service=service,
    )


@router.delete(
    "/{task_id}/access",
    response_model=DirectBindingOut,
)
async def revoke_task_access(
    task_id: uuid.UUID,
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
    return await _change_task_access(
        desired_present=False,
        task_id=task_id,
        body=body,
        idempotency_key=idempotency_key,
        request=request,
        ctx=ctx,
        service=service,
    )


@router.get("/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Return a single task row (RLS-scoped to caller's tenant).

    Cross-tenant rows are RLS-filtered to invisible — both ``not found``
    and ``foreign tenant`` collapse to 404 so we don't leak existence.
    """
    authorized = await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.VIEW,
    )
    t = await TasksRepo(session).get(task_id)
    if t is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id} not found",
        )
    return await _task_to_out(
        t,
        authorized.decision,
        ResourceProvenanceBuilder(session),
    )


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID,
    body: CancelBody,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Branch on the task's current status.

    Order of operations within each branch:
      1. UPDATE the row (so the DB state is the source of truth).
      2. Emit a ``task_events`` row (audit trail; SSE stream in T13
         consumes this).
      3. Commit the business state so every worker checkpoint sees the cancel.
      4. Defensive DBOS cancellation — best-effort. The durable business
         state remains authoritative and the batch body also polls it.
    """
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.CANCEL,
    )
    repo = TasksRepo(session)
    t = await repo.get(task_id)
    if t is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id} not found",
        )

    if t.status in _TERMINAL_OR_INFLIGHT_CANCEL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"task is {t.status}, cannot cancel",
        )

    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.CANCEL,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    tenant_uuid = uuid.UUID(ctx.tenant_id)

    if t.status == "queued":
        await session.execute(
            text(
                "UPDATE tasks SET status='cancelled', finished_at=now() "
                "WHERE id=:id"
            ),
            {"id": task_id},
        )
        await repo.insert_event(
            task_id, "terminal",
            {
                "schema_version": 1,
                "level": "info",
                "category": "task",
                "action": "task.cancelled",
                "message": "Queued task was cancelled before it started.",
                "task_status": "cancelled",
                "sandbox_status": "released",
                "scope": {"type": "task", "id": str(task_id), "name": None},
                "progress": None,
                "data": {"reason": "queued-cancel"},
                "error": None,
            },
            tenant_uuid,
        )
        await session.commit()
        await _rebind_request_organization(session, ctx)
        # Defensive revoke: a worker could grab this row between our
        # SELECT and our UPDATE (race window is short, since we're
        # inside one transaction, but a worker may claim the DBOS row
        # outside our business transaction). Best-effort: a queue-client
        # outage here is harmless — the row is already
        # ``cancelled`` in the DB.
        if t.background_job_id:
            try:
                await cancel_background_job_async(t.background_job_id)
            except Exception:
                pass
        return {"status": "cancelled"}

    if t.status in ("running", "resuming"):
        await session.execute(
            text("UPDATE tasks SET status='cancelling' WHERE id=:id"),
            {"id": task_id},
        )
        await repo.insert_event(
            task_id, "state",
            {
                "schema_version": 1,
                "level": "warning" if body.mode == "force" else "info",
                "category": "task",
                "action": "task.cancel_requested",
                "message": "Cancel requested.",
                "task_status": "cancelling",
                "sandbox_status": "running",
                "scope": {"type": "task", "id": str(task_id), "name": None},
                "progress": None,
                "data": {"mode": body.mode},
                "error": None,
            },
            tenant_uuid,
        )
        await session.commit()
        await _rebind_request_organization(session, ctx)
        if t.background_job_id:
            try:
                await cancel_background_job_async(t.background_job_id)
            except Exception:
                pass
        return {"status": "cancelling"}

    # Defensive — the CHECK constraint guarantees we never reach here,
    # but keep the explicit 409 so a future status addition fails loud
    # instead of silently returning 200.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"task is {t.status}, cannot cancel",
    )


@router.post("/{task_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_task(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Resume a resumable batch task in-place."""
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.RESUME,
    )
    repo = TasksRepo(session)
    t = await repo.get(task_id)
    if t is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id} not found",
        )
    if t.task_type != "batch_exec":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only batch_exec tasks can be resumed",
        )
    if t.status not in _RESUMABLE_BATCH_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"task is {t.status}, cannot resume",
        )

    result = t.result or {}
    if not (result.get("artifact_uris") or {}).get("jsonl"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task has no durable results.jsonl to resume from",
        )
    payload = t.payload or {}
    data_source = payload.get("data_source") or {}
    column_mapping = payload.get("column_mapping") or {}
    if not isinstance(data_source, dict) or not isinstance(column_mapping, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task payload is missing batch input configuration",
        )

    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.RESUME,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    # A DBOS workflow id is immutable/idempotent. Keep the user-visible Task id
    # stable and allocate a fresh workflow id for this resume attempt.
    resume_delivery_id = str(uuid.uuid4())
    await repo.update_status(
        task_id,
        status="resuming",
        background_job_id=resume_delivery_id,
        error=None,
        progress=0,
    )
    await repo.insert_event(
        task_id,
        "state",
        {
            "schema_version": 1,
            "level": "info",
            "category": "task",
            "action": "task.resume_requested",
            "message": "Resume requested.",
            "task_status": "resuming",
            "sandbox_status": "pending",
            "scope": {"type": "task", "id": str(task_id), "name": None},
            "progress": None,
            "data": {"resume_policy": "skip_success"},
            "error": None,
        },
        uuid.UUID(ctx.tenant_id),
    )
    await session.flush()

    await enqueue_background_job_in_transaction(
        session,
        "batch_exec",
        job_id=resume_delivery_id,
        queue="interactive",
        kwargs={"task_id": str(task_id)},
    )
    return {"status": "resuming", "task_id": str(task_id)}


@router.get("/{task_id}/events")
async def list_task_events(
    task_id: uuid.UUID,
    request: Request,
    after_seq: int | None = Query(default=None, ge=0),
    before_seq: int | None = Query(default=None, ge=1),
    event_type: list[str] = Query(default=[]),
    limit: int = Query(default=50, ge=1, le=200),
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.INSPECT_RUNS,
    )
    t = await TasksRepo(session).get(task_id)
    if t is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id} not found",
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
    repo = TasksRepo(session)
    descending = order == "desc"
    events = await repo.events_for_task(
        task_id=task_id,
        after_seq=after_seq,
        before_seq=before_seq,
        event_type=event_type or None,
        from_=from_,
        to=to,
        limit=limit + 1,
        descending=descending,
    )
    page = events[:limit]
    return {
        "items": [_event_to_out(ev) for ev in page],
        "limit": limit,
        "after_seq": after_seq,
        "before_seq": before_seq,
        "order": order,
        "next_cursor": page[-1].id if len(events) > limit and page else None,
        "latest_seq": await repo.latest_event_seq(task_id),
    }


@router.get("/{task_id}/stream")
async def stream_task_events(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Server-Sent Events stream of ``task_events`` for one task.

    Resume contract: clients send ``Last-Event-ID`` (a ``task_events.id``
    integer) to resume after a known cursor; the stream replays any
    rows with id > that cursor in id order before switching to the
    live tail. Absent / unparsable header is treated as ``0`` (replay
    everything).

    Ordering: ``task_events.id`` is BIGSERIAL — strictly monotonic
    per row insertion. The SELECT-replay is ``ORDER BY id``; the
    live tail dedupes on the same id; the worker publishes to
    Redis with the same id. End-to-end: strict, gap-free ordering.

    Tenant binding: the pre-check uses the request's tenant-bound DI
    session (RLS) — cross-tenant or absent tasks surface as 404. The
    stream then opens its own short ``session_scope(tenant_id=...)``
    sessions inside the generator so RLS keeps applying for every
    poll cycle.
    """
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.INSPECT_RUNS,
    )
    t = await TasksRepo(session).get(task_id)
    if not t:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id} not found",
        )

    try:
        last_event_id = int(request.headers.get("Last-Event-ID", "0"))
    except ValueError:
        last_event_id = 0

    return StreamingResponse(
        task_event_stream(
            task_id=task_id,
            last_event_id=last_event_id,
            tenant_id=ctx.tenant_id,
            redis_url=_config.redis.url,
            authorization_guard=lambda: authorization_lease_is_valid(
                auth=ctx,
                openfga_client=getattr(
                    request.app.state, "openfga_client", None
                ),
                resource=_task_resource(ctx, task_id),
                action=Action.INSPECT_RUNS,
            ),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )


@router.get("/{task_id}/download")
async def download_results(
    task_id: uuid.UUID,
    request: Request,
    format: Literal["csv", "jsonl", "xlsx"] = "csv",
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Download task results as CSV, JSONL, or an on-demand Excel workbook.

    Returns 404 if:
      * the task does not exist (or is RLS-bound to another tenant), or
      * the task has not produced a ``results_uri`` yet (queued / running
        / cancelled-before-upload).

    Private results always stream through the authorized application gateway.
    A direct S3 presigned URL would remain usable after resource revocation and
    would expose plaintext bytes outside the gateway.  The synchronous Object
    Store iterator is consumed by Starlette's worker thread pool, so the API
    holds at most one bounded chunk instead of loading the whole result.
    """
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.EXPORT,
    )
    t = await TasksRepo(session).get(task_id)
    if t is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no results to download",
        )

    summary = t.result if isinstance(t.result, dict) else {}
    artifacts = summary.get("artifact_uris")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    if format == "csv":
        artifact_uri = artifacts.get("csv") or t.results_uri
        media_type = "text/csv; charset=utf-8"
        extension = "csv"
    else:
        # JSONL is the canonical structured row ledger. Excel is generated
        # from it only when requested, avoiding a permanently duplicated
        # workbook for every task.
        artifact_uri = artifacts.get("jsonl")
        media_type = "application/x-ndjson" if format == "jsonl" else (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        extension = "jsonl" if format == "jsonl" else "xlsx"
    if not isinstance(artifact_uri, str) or not artifact_uri:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no results to download",
        )

    store = get_object_store()
    key = uri_to_key(artifact_uri)

    if format == "xlsx":
        try:
            raw_jsonl = await asyncio.to_thread(store.fetch_bytes, key)
            rows = [
                json.loads(line)
                for line in raw_jsonl.decode("utf-8").splitlines()
                if line.strip()
            ]
            payload = t.payload if isinstance(t.payload, dict) else {}
            columns = payload.get("output_columns")
            if not isinstance(columns, list):
                columns = None
            content, media_type = await asyncio.to_thread(
                serialize_results,
                rows,
                path="results.xlsx",
                sheet_name="Results",
                columns=columns,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no results to download",
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="stored task results are invalid",
            ) from exc
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="results-{task_id}.{extension}"'
                ),
                "Cache-Control": "private, no-store",
            },
        )

    chunks = iter(store.iter_bytes(key))
    try:
        # Pre-fetch one bounded chunk so a missing object still maps to a
        # deterministic 404 before response headers are sent.
        first = await asyncio.to_thread(_next_object_chunk, chunks)
    except KeyError as exc:
        # FIX-2: the row carries a ``results_uri`` but the blob is gone
        # (GC'd / never written / wrong provider). Surface 404 — mirror
        # the no-``results_uri`` branch above — rather than letting the
        # KeyError bubble into a 500.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no results to download",
        ) from exc
    return StreamingResponse(
        _stream_object_chunks(first, chunks),
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="results-{task_id}.{extension}"'
            ),
            "Cache-Control": "private, no-store",
        },
    )
