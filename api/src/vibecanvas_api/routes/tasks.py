"""Task Center routes.

Task Center tracks user-managed ``batch_exec`` and ``scheduled_run`` work.
Online API/webhook deployment invocations and KB indexing do not create
Task rows. ``task_events.event_type`` uses the unified protocol:
``state | progress | log | result | terminal``; concrete actions and
status snapshots live in the JSON payload.
"""
from __future__ import annotations

from vibecanvas_api.services.task_notifications import NotificationPolicy

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
    major: str | None = None
    version: str | None = None
    enabled: bool = True
    schedule_type: str = "interval"
    run_at: datetime | None = None
    interval_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str = "UTC"
    start_at: datetime | None = None
    end_at: datetime | None = None
    input_preset: dict = Field(default_factory=dict)
    mount_enabled: bool = False
    notification_policy: NotificationPolicy = Field(default_factory=lambda: dict(DEFAULT_NOTIFICATION_POLICY))


class ScheduledRunPatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    major: str | None = None
    version: str | None = None
    enabled: bool | None = None
    schedule_type: str | None = None
    run_at: datetime | None = None
    interval_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    input_preset: dict | None = None
    mount_enabled: bool | None = None
    notification_policy: NotificationPolicy | None = None


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
        "workflow_version": getattr(t, "workflow_version", None) if can_view_content else None,
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
    source: Literal["all", "created", "shared"] = "all",
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
    from vibecanvas_api.services.shared_inventory import inventory_groups, inventory_context
    groups = await inventory_groups(service=service, principal=principal, context=context,
        resource_type=ResourceType.TASK, local_ids=authorized_ids)
    original = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one()
    output_items = []
    total = 0
    try:
        for owner, identifiers in groups.items():
            await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": owner})
            items, count = await TasksRepo(session).list_for_tenant(task_ids=identifiers,
                status=status or None, task_type=task_type or None, workflow_id=workflow_id, search=q,
                creator_user_id=ctx.user_id if source != "all" else None,
                exclude_creator=source == "shared", limit=offset + limit, offset=0)
            total += count
            provenance = ResourceProvenanceBuilder(session)
            for item in items:
                decision = await service.check(principal, Action.VIEW_METADATA,
                    ResourceRef(ResourceType.TASK, str(item.id), owner),
                    inventory_context(context, owner, "task", str(item.id)))
                if not decision.allowed:
                    total -= 1
                    continue
                output = await _task_to_out(item, decision, provenance)
                output["created_by_me"] = str(item.user_id) == str(ctx.user_id)
                output_items.append(output)
    finally:
        await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": original or ""})
    output_items.sort(key=lambda item: item["id"])
    output_items.sort(key=lambda item: item.get("submitted_at") or "", reverse=True)
    return {"items": output_items[offset:offset + limit], "total": total, "limit": limit, "offset": offset}


@router.get("/summary")
async def tasks_summary(
    request: Request,
    workflow_id: str | None = None,
    source: Literal["all", "created", "shared"] = "all",
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
    from vibecanvas_api.services.shared_inventory import inventory_groups
    groups = await inventory_groups(service=service, principal=principal_for_auth(ctx),
        context=context_for_auth(ctx, request), resource_type=ResourceType.TASK, local_ids=authorized_ids)
    original = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one()
    summary = {}
    try:
        for owner, identifiers in groups.items():
            await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": owner})
            counts = await TasksRepo(session).summary_for_tenant(task_ids=identifiers,
                task_type=task_type, workflow_id=workflow_id,
                creator_user_id=ctx.user_id if source != "all" else None, exclude_creator=source == "shared")
            for key, value in counts.items():
                summary[key] = summary.get(key, 0) + value
    finally:
        await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": original or ""})
    return summary


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
    if body.schedule_type not in {"interval", "cron", "once"}:
        raise HTTPException(status_code=422, detail="schedule_type must be interval, cron or once")
    if body.schedule_type == "once" and any(v is not None for v in (
        body.cron_expr, body.interval_seconds, body.start_at, body.end_at,
    )):
        raise HTTPException(status_code=422, detail="once uses run_at, not recurring timing fields")
    if body.schedule_type != "once" and body.run_at is not None:
        raise HTTPException(status_code=422, detail="run_at is only valid for once schedules")
    if body.schedule_type == "interval" and (body.interval_seconds or 0) <= 0:
        raise HTTPException(status_code=422, detail="interval_seconds must be positive")
    if body.schedule_type == "cron" and not body.cron_expr:
        raise HTTPException(status_code=422, detail="cron_expr is required")
    from vibecanvas_api.authorization.dependencies import authorized_resource_scope
    from vibecanvas_api.services.task_snapshots import freeze_workflow
    async with authorized_resource_scope(request=request, auth=ctx, session=session,
            resource_type=ResourceType.WORKFLOW, resource_id=body.workflow_id, action=Action.EXECUTE):
        workflow_tenant_id = await session.scalar(text("SELECT current_setting('app.tenant_id', true)"))
        snapshot = await freeze_workflow(session, ctx.user_id, body.workflow_id,
                                         major=body.major, version=body.version)
    try:
        next_run_at = (
            compute_next_run_at(
                schedule_type=body.schedule_type,
                timezone_name=body.timezone,
                interval_seconds=body.interval_seconds,
                cron_expr=body.cron_expr,
                start_at=body.start_at,
                run_at=body.run_at,
            )
        )
        if not body.enabled:
            next_run_at = None
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
            status="active",  # Pausing dispatch must not revoke a live execution.
        )
        credential_ids = await bind_workflow_credentials(
            session,
            tenant_id=uuid.UUID(ctx.tenant_id),
            service_account_id=service_account_id,
            created_by=ctx.user_id,
            workflow=snapshot["workflow"],
        )
        task, schedule = await repo.create_schedule(
            task_id=task_id,
            schedule_id=schedule_id,
            tenant_id=uuid.UUID(ctx.tenant_id),
            user_id=uuid.UUID(ctx.user_id),
            workflow_id=body.workflow_id,
            workflow_tenant_id=uuid.UUID(workflow_tenant_id),
            name=body.name.strip() or "Scheduled run",
            enabled=body.enabled,
            schedule_type=body.schedule_type,
            run_at=body.run_at,
            cron_expr=body.cron_expr,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone or "UTC",
            input_preset=body.input_preset or {},
            mount_enabled=body.mount_enabled,
            notification_policy=merge_notification_policy(body.notification_policy),
            next_run_at=next_run_at,
            end_at=normalized_end_at,
            service_account_id=service_account_id,
            workflow_selector={"version": snapshot["version"]},
            start_at=body.start_at.isoformat() if body.start_at else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
                workflow_organization_id=workflow_tenant_id,
                credential_ids=credential_ids,
                resource_owners=await ServiceAccountsRepo(session).resource_owners(service_account_id),
            )
        ),
        operation_id=uuid.uuid4().hex,
        source="scheduled-task-create",
    )
    await session.commit()
    accepted = {
        "task": {"id": str(task_id), "task_type": "scheduled_run", "workflow_id": body.workflow_id, "status": task.status},
        "schedule": schedule_to_out(schedule),
        "authorization_pending": True,
    }
    try:
        await apply_committed_structural_mutations(coordinator, mutation_ids)
        await _rebind_request_organization(session, ctx)
    except Exception:
        return accepted
    try:
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
            return accepted
        return {
            "task": await _task_to_out(
                task,
                decision,
                ResourceProvenanceBuilder(session),
            ),
            "schedule": schedule_to_out(schedule),
        }
    except Exception:
        return accepted


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
    schedule = await repo.get_schedule_by_task(task_id, for_update=True)
    if task is None or schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    fields = {}
    for key in ("name", "schedule_type", "interval_seconds", "cron_expr", "timezone", "input_preset", "mount_enabled", "end_at", "run_at"):
        if key in body.model_fields_set:
            value = getattr(body, key)
            if value is not None or key in {"end_at", "interval_seconds", "cron_expr", "run_at"}:
                fields[key] = value
    if body.notification_policy is not None:
        fields["notification_policy"] = merge_notification_policy(body.notification_policy)
    if "start_at" in body.model_fields_set:
        fields["start_at"] = body.start_at.isoformat() if body.start_at else None
    if body.major or body.version:
        from vibecanvas_api.services.task_snapshots import freeze_workflow
        from vibecanvas_api.authorization.dependencies import authorized_resource_scope
        async with authorized_resource_scope(request=request, auth=ctx, session=session,
                resource_type=ResourceType.WORKFLOW, resource_id=schedule.workflow_id, action=Action.EXECUTE):
            snapshot = await freeze_workflow(session, ctx.user_id, schedule.workflow_id, major=body.major, version=body.version)
        fields["workflow_selector"] = {"version": snapshot["version"]}
    enabled = schedule.enabled if body.enabled is None else body.enabled
    schedule_type = fields.get("schedule_type", schedule.schedule_type)
    if "schedule_type" in fields:
        if schedule_type != "cron":
            fields["cron_expr"] = None
        if schedule_type != "interval":
            fields["interval_seconds"] = None
        if schedule_type != "once":
            fields["run_at"] = None
        else:
            fields.update(start_at=None, end_at=None)
    timing_changed = bool(body.model_fields_set & {"schedule_type", "interval_seconds", "cron_expr", "timezone", "start_at", "end_at", "enabled", "run_at"})
    next_run_at = schedule.next_run_at
    try:
        if schedule_type == "once" and any(fields.get(k, getattr(schedule, k, None)) is not None
                                           for k in ("cron_expr", "interval_seconds", "start_at", "end_at")):
            raise ValueError("once uses run_at, not recurring timing fields")
        if schedule_type != "once" and fields.get("run_at", getattr(schedule, "run_at", None)) is not None:
            raise ValueError("run_at is only valid for once schedules")
        if timing_changed:
            saved_start = fields.get("start_at", getattr(schedule, "start_at", None))
            next_run_at = compute_next_run_at(
                schedule_type=schedule_type, timezone_name=fields.get("timezone", schedule.timezone),
                interval_seconds=fields.get("interval_seconds", schedule.interval_seconds),
                cron_expr=fields.get("cron_expr", schedule.cron_expr),
                start_at=datetime.fromisoformat(saved_start) if saved_start else None,
                run_at=fields.get("run_at", getattr(schedule, "run_at", None)),
            ) if enabled or body.model_fields_set != {"enabled"} else None
            if not enabled:
                next_run_at = None
        end_at = fields.get("end_at", schedule.end_at)
        if end_at is not None and next_run_at is not None and end_at < next_run_at:
            raise ValueError("end_at must not be earlier than the next scheduled run")
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
    if enabled and not schedule.enabled:
        # Enabling through settings starts future executions just like resume.
        # Editing configuration must not bypass the separate execution grant.
        await _authorize_task(
            request=request, ctx=ctx, service=service, task_id=task_id,
            action=Action.RESUME,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        )
    schedule = await repo.update_schedule(schedule.id, **fields)
    if task.service_account_id is not None:
        await ServiceAccountsRepo(session).set_status(
            task.service_account_id,
            status="active",
        )
    await repo.refresh_scheduled_task(task_id)
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
    schedule = await repo.get_schedule_by_task(task_id, for_update=True)
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
    await repo.refresh_scheduled_task(task_id)
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
    schedule = await repo.get_schedule_by_task(task_id, for_update=True)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    try:
        next_run_at = compute_next_run_at(
            schedule_type=schedule.schedule_type,
            timezone_name=schedule.timezone,
            interval_seconds=schedule.interval_seconds,
            cron_expr=schedule.cron_expr,
            start_at=datetime.fromisoformat(schedule.start_at) if getattr(schedule, "start_at", None) else None,
            run_at=getattr(schedule, "run_at", None),
        )
        if schedule.end_at is not None and next_run_at > schedule.end_at:
            raise ValueError("Schedule end time has passed; update it before resuming.")
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
    await repo.refresh_scheduled_task(task_id)
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
    schedule = await repo.get_schedule_by_task(task_id, for_update=True)
    if task is None or schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
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
        tenant_id=schedule.tenant_id,
        schedule_id=schedule.id,
        workflow_id=schedule.workflow_id,
        run_key=run_key,
        trigger_type="manual",
        input_snapshot=schedule.input_preset or {},
    )
    if execution is None:
        raise HTTPException(status_code=409, detail="duplicate scheduled execution")
    await repo.refresh_scheduled_task(task_id)
    await session.flush()
    await _send_scheduled_execution(
        session=session,
        task_id=task_id,
        schedule_id=schedule.id,
        execution_id=execution_id,
        tenant_id=str(schedule.tenant_id),
        user_id=str(schedule.user_id),
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
    schedule = await repo.get_schedule_by_task(task_id, for_update=True)
    if task is None or schedule is None:
        raise HTTPException(status_code=404, detail=f"scheduled run {task_id} not found")
    if await repo.has_active_scheduled_execution(schedule.id):
        raise HTTPException(status_code=409, detail="cancel active execution before deleting schedule")
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    return await _delete_task_record(task_id, task, request, ctx, session)


@router.delete("/{task_id}")
async def delete_batch_task(
    task_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(request=request, ctx=ctx, service=service, task_id=task_id, action=Action.DELETE)
    task = await TasksRepo(session).get(task_id, for_update=True)
    if task is None or task.task_type != "batch_exec":
        raise HTTPException(status_code=404, detail="Batch task not found.")
    from vibecanvas_api.services.batch_evaluation import active_evaluation
    if active_evaluation(task):
        raise HTTPException(409, "Wait for the current evaluation before deleting this task.")
    if task.status not in {"finished", "finished_with_errors", "failed", "interrupted", "cancelled"} or task.worker_recovery_pending:
        raise HTTPException(status_code=409, detail="Stop the active execution and wait for a terminal state before deleting this task.")
    await _authorize_task(request=request, ctx=ctx, service=service, task_id=task_id,
        action=Action.DELETE, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    return await _delete_task_record(task_id, task, request, ctx, session)


async def _delete_task_record(task_id, task, request, ctx, session):
    """Delete tracking/history and revoke its service identity, not Workflow exports."""
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
            organization_id=str(task.tenant_id),
            service_account_id=str(task.service_account_id),
            created_by=str(task.user_id),
            owner_resource_type="task",
            owner_resource_id=str(task_id),
            workflow_id=str(task.workflow_id),
            workflow_organization_id=str(task.workflow_tenant_id),
            credential_ids=credential_ids,
            resource_owners=await account_repo.resource_owners(task.service_account_id),
        )
        await account_repo.set_status(
            task.service_account_id,
            status="deleted",
        )
    await session.execute(text("DELETE FROM tasks WHERE id=:id"), {"id": task_id})
    coordinator = mutation_coordinator_for_request(
        request,
        str(task.tenant_id),
    )
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=ctx.user_id,
        before=(
            resource_root_edges(
                organization_id=str(task.tenant_id),
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
        source="task-delete",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return {"status": "deleted"}


@router.get("/scheduled-runs/{task_id}/executions/{execution_id}/download")
async def download_scheduled_execution_results(
    task_id: uuid.UUID,
    execution_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(request=request, ctx=ctx, service=service, task_id=task_id, action=Action.EXPORT)
    execution = await get_scheduled_run_execution(task_id, execution_id, request=request, ctx=ctx, session=session, service=service)
    if execution["status"] not in {"succeeded", "failed", "cancelled", "skipped"} or execution.get("result") is None:
        raise HTTPException(status_code=409, detail="Execution results are not available. Inspect status and logs; do not resubmit.")
    body = {"task_id": str(task_id), "execution_id": str(execution_id), "status": execution["status"],
            "version": execution.get("version"), "result": execution["result"], "execution_error": execution.get("error")}
    return Response(json.dumps(body, ensure_ascii=False), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="execution-{execution_id}.json"'})


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
    events = await repo.events_for_task(task_id=task_id, execution_id=execution_id, limit=limit)
    return {"items": [_event_to_out(x) for x in events], "limit": limit}


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
    schedule = await repo.get_schedule_by_task(task_id, for_update=True)
    execution = await repo.get_scheduled_execution(execution_id, for_update=True)
    if schedule is None or execution is None or execution.schedule_id != schedule.id:
        raise HTTPException(status_code=404, detail=f"execution {execution_id} not found")
    if execution.status not in {"queued", "running", "cancelling"}:
        raise HTTPException(status_code=409, detail=f"execution is {execution.status}, cannot cancel")
    await _authorize_task(
        request=request,
        ctx=ctx,
        service=service,
        task_id=task_id,
        action=Action.CANCEL,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    if execution.status == "cancelling":
        return {"status": "cancelling"}
    queued = execution.status == "queued"
    state = "cancelled" if queued else "cancelling"
    cancelled_at = datetime.now(timezone.utc)
    await repo.update_scheduled_execution(
        execution_id,
        status=state,
        finished_at=cancelled_at if queued else None,
        error="Cancelled by user.",
    )
    task = await repo.refresh_scheduled_task(task_id)
    if queued:
        return {"status": state}
    await repo.insert_event(
        task_id,
        "state",
        {
            "schema_version": 1,
            "level": "warning",
            "category": "scheduled_run",
            "action": f"scheduled_run.{state}",
            "message": "Scheduled execution cancellation requested.",
            "task_status": task.status,
            "sandbox_status": "releasing",
            "scope": {"type": "scheduled_run_execution", "id": str(execution_id), "name": None},
            "progress": None,
            "data": {"execution_id": str(execution_id), "schedule_id": str(schedule.id)},
            "error": None,
        },
        task.tenant_id,
    )
    return {"status": state}


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


@router.get("/{task_id}/workflow-preview")
async def preview_task_workflow(
    task_id: uuid.UUID, workflow_id: str, version: str, request: Request,
    ctx: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(request=request, ctx=ctx, service=service,
                          task_id=task_id, action=Action.VIEW)
    repo = TasksRepo(session)
    task = await repo.get(task_id)
    if task is None:
        raise HTTPException(404, "task_not_found")
    from vibecanvas_api.services.instance_workflow_preview import task_workflow_preview
    return await task_workflow_preview(session, ctx.user_id, task,
        await repo.get_schedule_by_task(task_id), workflow_id=workflow_id, version=version)


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
    tenant_uuid = t.tenant_id

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
    t = await repo.get(task_id, for_update=True)
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

    from vibecanvas_api.services.batch_evaluation import active_evaluation
    if active_evaluation(t):
        raise HTTPException(409, "Wait for the current evaluation before resuming inference.")
    result = t.result or {}
    if result.get("can_resume") is False:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
            detail="This attempt cannot be resumed safely. Inspect its error and external side effects before submitting new work.")
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
        t.tenant_id,
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
    execution_id: uuid.UUID | None = None,
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
    if execution_id is not None:
        schedule = await repo.get_schedule_by_task(task_id)
        execution = await repo.get_scheduled_execution(execution_id)
        if schedule is None or execution is None or execution.schedule_id != schedule.id:
            raise HTTPException(status_code=404, detail="Execution not found in this task.")
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
        execution_id=execution_id,
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
            tenant_id=str(t.tenant_id),
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


class ResultQuery(BaseModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    search: str = Field(default="", max_length=1000)
    row_status: str = ""
    sort: str = "index"
    descending: bool = False
    filters: dict[str, str] = Field(default_factory=dict)


@router.post("/{task_id}/results/query")
async def query_results(
    task_id: uuid.UUID, body: ResultQuery, request: Request,
    ctx: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    from vibecanvas_api.services.batch_evaluation import load_results, result_uri
    await _authorize_task(request=request, ctx=ctx, service=service, task_id=task_id, action=Action.VIEW)
    task = await TasksRepo(session).get(task_id)
    if task is None:
        raise HTTPException(404, "Task not found.")
    rows, version = await asyncio.to_thread(load_results, result_uri(task))
    total = len(rows)
    counts = {name: sum(row.get("status") == name for row in rows) for name in ("success", "error", "cancelled")}
    def cell(row, key):
        value = row.get(key)
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value if value is not None else "")
    if body.search:
        needle = body.search.casefold()
        rows = [r for r in rows if needle in json.dumps(r, ensure_ascii=False).casefold()]
    if body.row_status:
        rows = [r for r in rows if r.get("status") == body.row_status]
    for key, needle in body.filters.items():
        rows = [r for r in rows if needle.casefold() in cell(r, key).casefold()]
    numeric = body.sort in {"index", "i", "attempt", "execution_time", "elapsed_ms"}
    rows.sort(key=lambda r: (float(r.get(body.sort) or 0) if numeric else cell(r, body.sort).casefold()), reverse=body.descending)
    return {"rows": rows[body.offset:body.offset + body.limit], "filtered": len(rows), "total": total,
            "counts": counts, "version": version, "partial": task.status not in {"finished", "finished_with_errors"}}


@router.get("/{task_id}/evaluation")
async def get_evaluation(
    task_id: uuid.UUID, request: Request,
    ctx: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    from vibecanvas_api.services.batch_evaluation import TERMINAL
    await _authorize_task(request=request, ctx=ctx, service=service, task_id=task_id, action=Action.VIEW)
    task = await TasksRepo(session).get(task_id)
    if task is None or task.task_type != "batch_exec":
        raise HTTPException(404, "Batch task not found.")
    return {"config": task.payload.get("evaluation") or {"enabled": False, "script": ""},
            "records": task.payload.get("evaluations", []),
            "result_version": (task.result or {}).get("result_version"),
            "ready": task.status in TERMINAL and bool(((task.result or {}).get("artifact_uris") or {}).get("jsonl"))}


from vibecanvas_api.services.batch_evaluation import EvaluationConfig


@router.put("/{task_id}/evaluation")
async def save_evaluation(
    task_id: uuid.UUID, body: EvaluationConfig, request: Request,
    ctx: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_task(request=request, ctx=ctx, service=service, task_id=task_id, action=Action.UPDATE)
    repo = TasksRepo(session)
    task = await repo.get(task_id, for_update=True)
    if task is None or task.task_type != "batch_exec":
        raise HTTPException(404, "Batch task not found.")
    await repo.update_status(task_id, payload={**task.payload, "evaluation": body.model_dump()})
    return body.model_dump()


@router.post("/{task_id}/evaluation", status_code=202)
async def start_evaluation(
    task_id: uuid.UUID, request: Request,
    ctx: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    from vibecanvas_api.services.batch_evaluation import queue_evaluation
    await _authorize_task(request=request, ctx=ctx, service=service, task_id=task_id, action=Action.EXECUTE)
    repo = TasksRepo(session)
    task = await repo.get(task_id, for_update=True)
    if task is None:
        raise HTTPException(404, "Task not found.")
    try:
        return await queue_evaluation(session, task)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
