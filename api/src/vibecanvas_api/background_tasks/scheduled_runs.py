"""Background tasks for user-facing scheduled workflow runs."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import redis
import structlog
from sqlalchemy import select

from vibecanvas_api.authorization.types import ResourceType
from vibecanvas_api.config import config
from vibecanvas_api.services.background_queue import (
    enqueue_background_job_in_transaction,
)
from vibecanvas_api.services.llm_credentials_inject import inject_into_run_context_async
from vibecanvas_api.services.queue_routing import route_for
from vibecanvas_api.services.redis_channels import (
    task_event_channel,
    task_event_envelope,
)
from vibecanvas_api.services.sandbox.coordinator import (
    dispose_sandbox_rpc_client,
)
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.scheduled_runs import compute_next_run_at, utc_now
from vibecanvas_api.services.workflow_sandbox_runner import stream_workflow_job
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.models_tasks import (
    ScheduledRunExecution,
    TaskSchedule,
)
from vibecanvas_api.storage.repo_service_accounts import (
    ServiceAccountLease,
    ServiceAccountsRepo,
)
from vibecanvas_api.storage.sync_repo import SyncWorkflowRepo
from vibecanvas_api.storage.db import dispose_engine
from vibecanvas_api.storage.sync_session import (
    current_sync_tenant_id,
    run_in_short_session,
    short_admin_session,
)

logger = structlog.get_logger(__name__)
SCHEDULED_RUN_DISPATCH_INTERVAL_SEC = 60.0


def _publish(task_id: uuid.UUID, tenant_id: uuid.UUID, message: dict) -> None:
    try:
        r = redis.from_url(
            config.redis.url,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        r.publish(
            task_event_channel(tenant_id, task_id),
            json.dumps(
                task_event_envelope(
                    organization_id=tenant_id,
                    task_id=task_id,
                    event=message,
                ),
                default=str,
            ),
        )
    except Exception:
        pass


def _emit(task_id: uuid.UUID, tenant_id: uuid.UUID, event_type: str, payload: dict) -> None:
    async def _runner(session) -> int:
        return await TasksRepo(session).insert_event(task_id, event_type, payload, tenant_id)

    ev_id = run_in_short_session(_runner)
    _publish(
        task_id,
        tenant_id,
        {"id": ev_id, "event_type": event_type, "payload": payload},
    )


def _update_task(task_id: uuid.UUID, **fields: object) -> None:
    async def _runner(session) -> None:
        await TasksRepo(session).update_status(task_id, **fields)

    run_in_short_session(_runner)


def _update_execution(execution_id: uuid.UUID, **fields: object) -> None:
    async def _runner(session) -> None:
        await TasksRepo(session).update_scheduled_execution(execution_id, **fields)

    run_in_short_session(_runner)


def _update_schedule(schedule_id: uuid.UUID, **fields: object) -> None:
    async def _runner(session) -> None:
        await TasksRepo(session).update_schedule(schedule_id, **fields)

    run_in_short_session(_runner)


def _snapshot_schedule(schedule_id: uuid.UUID) -> dict:
    async def _runner(session) -> dict:
        repo = TasksRepo(session)
        schedule = await repo.get_schedule(schedule_id)
        if schedule is None:
            return {}
        task = await repo.get(schedule.task_id)
        return {
            "schedule_id": str(schedule.id),
            "task_id": str(schedule.task_id),
            "tenant_id": str(schedule.tenant_id),
            "user_id": str(schedule.user_id),
            "workflow_id": schedule.workflow_id,
            "name": schedule.name,
            "enabled": schedule.enabled,
            "task_status": task.status if task else None,
            "notification_policy": schedule.notification_policy or {},
            "service_account_id": (
                str(schedule.service_account_id)
                if schedule.service_account_id is not None
                else None
            ),
        }

    return run_in_short_session(_runner)


def _scheduled_execution_lease(
    *,
    task_id: uuid.UUID,
    schedule_id: uuid.UUID,
    workflow_id: str,
) -> ServiceAccountLease:
    async def _runner(session) -> ServiceAccountLease:
        repo = TasksRepo(session)
        task = await repo.get(task_id)
        schedule = await repo.get_schedule(schedule_id)
        if (
            task is None
            or schedule is None
            or schedule.task_id != task_id
            or schedule.workflow_id != workflow_id
            or task.workflow_id != workflow_id
            or task.service_account_id is None
            or schedule.service_account_id != task.service_account_id
        ):
            raise LookupError("service_account_unavailable")
        return await ServiceAccountsRepo(session).require_active_lease(
            service_account_id=task.service_account_id,
            owner_resource_type="task",
            owner_resource_id=str(task_id),
        )

    return run_in_short_session(_runner)


def _schedule_task_payload(schedule: dict, *, next_run_at: datetime | None = None) -> dict:
    payload = {
        "name": schedule["name"],
        "schedule_id": str(schedule["id"]),
        "schedule_type": schedule["schedule_type"],
        "cron_expr": schedule.get("cron_expr"),
        "interval_seconds": schedule.get("interval_seconds"),
        "timezone": schedule.get("timezone") or "UTC",
        "next_run_at": next_run_at.isoformat() if next_run_at else None,
        "end_at": schedule.get("end_at").isoformat() if schedule.get("end_at") else None,
        "last_status": schedule.get("last_status"),
        "notification_policy": schedule.get("notification_policy") or {},
    }
    return payload


def dispatch_due_scheduled_runs() -> None:
    asyncio.run(_dispatch_due_scheduled_runs())


async def _dispatch_due_scheduled_runs(limit: int = 50) -> None:
    now = utc_now()
    async with short_admin_session() as session:
        rows = list((await session.execute(
            select(TaskSchedule)
            .where(
                TaskSchedule.enabled.is_(True),
                TaskSchedule.next_run_at.is_not(None),
                TaskSchedule.next_run_at <= now,
                (
                    TaskSchedule.end_at.is_(None)
                    | (TaskSchedule.end_at > now)
                ),
            )
            .order_by(TaskSchedule.next_run_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )).scalars().all())
        repo = TasksRepo(session)

        for schedule in rows:
            schedule = await repo.get_schedule(schedule.id)
            if schedule is None or schedule.next_run_at is None:
                continue
            next_run = compute_next_run_at(
                schedule_type=schedule.schedule_type,
                timezone_name=schedule.timezone,
                interval_seconds=schedule.interval_seconds,
                cron_expr=schedule.cron_expr,
                base=now,
            )
            run_key = f"{schedule.id}:{schedule.next_run_at.isoformat()}"
            active = (await session.execute(
                select(ScheduledRunExecution.id).where(
                    ScheduledRunExecution.schedule_id == schedule.id,
                    ScheduledRunExecution.status.in_(("queued", "running")),
                ).limit(1)
            )).first()
            schedule_snapshot = {
                "id": schedule.id,
                "name": schedule.name,
                "schedule_type": schedule.schedule_type,
                "cron_expr": schedule.cron_expr,
                "interval_seconds": schedule.interval_seconds,
                "timezone": schedule.timezone,
                "end_at": schedule.end_at,
                "last_status": schedule.last_status,
                "notification_policy": schedule.notification_policy,
            }
            task = await repo.get(schedule.task_id)
            if task is None:
                continue
            task_payload = dict(task.payload or {})
            task_payload.update(
                _schedule_task_payload(schedule_snapshot, next_run_at=next_run)
            )

            if active:
                execution_id = uuid.uuid4()
                skipped = await repo.create_scheduled_execution(
                    execution_id=execution_id,
                    tenant_id=schedule.tenant_id,
                    schedule_id=schedule.id,
                    workflow_id=schedule.workflow_id,
                    run_key=run_key,
                    trigger_type="scheduled",
                    input_snapshot=schedule.input_preset or {},
                    status="skipped",
                )
                if skipped is not None:
                    await repo.update_scheduled_execution(
                        execution_id,
                        error=(
                            "Skipped because a previous execution is still active."
                        ),
                    )
                await repo.update_schedule(
                    schedule.id,
                    next_run_at=next_run,
                    last_run_at=now,
                    last_status="skipped",
                )
                await repo.update_status(schedule.task_id, payload=task_payload)
                continue

            execution_id = uuid.uuid4()
            inserted = await repo.create_scheduled_execution(
                execution_id=execution_id,
                tenant_id=schedule.tenant_id,
                schedule_id=schedule.id,
                workflow_id=schedule.workflow_id,
                run_key=run_key,
                trigger_type="scheduled",
                input_snapshot=schedule.input_preset or {},
            )
            await repo.update_schedule(schedule.id, next_run_at=next_run)
            await repo.update_status(schedule.task_id, payload=task_payload)
            if inserted is not None:
                await enqueue_background_job_in_transaction(
                    session,
                    "scheduled_runs.execute",
                    job_id=str(execution_id),
                    queue=route_for("scheduled_run"),
                    kwargs={"execution_id": str(execution_id)},
                )


def execute_scheduled_run(
    *,
    task_id: str,
    schedule_id: str,
    execution_id: str,
    tenant_id: str,
    user_id: str,
    workflow_id: str,
) -> None:
    current_sync_tenant_id.set(tenant_id)

    async def _run_on_isolated_worker_loop() -> None:
        # Each DBOS Step gets a fresh ``asyncio.run`` loop. Drop pooled state
        # left by a previous Step before using async DB/gRPC helpers, then
        # close this Step's resources before its loop disappears. Otherwise a
        # second scheduled execution inherits Futures bound to the first loop.
        await dispose_engine(close=False)
        try:
            await _execute_scheduled_run(
                task_id=uuid.UUID(task_id),
                schedule_id=uuid.UUID(schedule_id),
                execution_id=uuid.UUID(execution_id),
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_id=workflow_id,
            )
        finally:
            await dispose_sandbox_rpc_client()
            await dispose_engine()

    asyncio.run(_run_on_isolated_worker_loop())


async def _execute_scheduled_run(
    *,
    task_id: uuid.UUID,
    schedule_id: uuid.UUID,
    execution_id: uuid.UUID,
    tenant_id: str,
    user_id: str,
    workflow_id: str,
) -> None:
    tenant_uuid = uuid.UUID(tenant_id)
    if await _execution_cancelled(execution_id):
        logger.info(
            "scheduled_run_execution_cancelled_before_start",
            task_id=str(task_id),
            schedule_id=str(schedule_id),
            execution_id=str(execution_id),
        )
        return
    try:
        lease = _scheduled_execution_lease(
            task_id=task_id,
            schedule_id=schedule_id,
            workflow_id=workflow_id,
        )
    except LookupError:
        finished = datetime.now(timezone.utc)
        _update_execution(
            execution_id,
            status="failed",
            finished_at=finished,
            error="service_account_unavailable",
        )
        _update_task(
            task_id,
            status="failed",
            error="service_account_unavailable",
            finished_at=finished,
        )
        _emit(task_id, tenant_uuid, "terminal", {
            "schema_version": 1,
            "level": "error",
            "category": "scheduled_run",
            "action": "scheduled_run.failed",
            "message": "The scheduled execution identity is unavailable.",
            "task_status": "failed",
            "sandbox_status": "released",
            "scope": {"type": "scheduled_run_execution", "id": str(execution_id), "name": None},
            "progress": None,
            "data": {"execution_id": str(execution_id), "schedule_id": str(schedule_id)},
            "error": {
                "code": "service_account_unavailable",
                "message": "The scheduled execution identity is unavailable.",
                "retryable": False,
                "details": {},
            },
        })
        return
    user_id = str(lease.created_by)
    started = datetime.now(timezone.utc)
    _update_execution(execution_id, status="running", started_at=started)
    _update_task(task_id, status="running", started_at=started, error=None)
    _emit(task_id, tenant_uuid, "state", {
        "schema_version": 1,
        "level": "info",
        "category": "scheduled_run",
        "action": "scheduled_run.started",
        "message": "Scheduled run started.",
        "task_status": "running",
        "sandbox_status": "running",
        "scope": {"type": "scheduled_run_execution", "id": str(execution_id), "name": None},
        "progress": None,
        "data": {"execution_id": str(execution_id), "schedule_id": str(schedule_id)},
        "error": None,
    })

    final_status = "succeeded"
    result_payload: dict | None = None
    error_message: str | None = None
    try:
        async def _input_snapshot(session) -> dict:
            ex = await TasksRepo(session).get_scheduled_execution(execution_id)
            return (ex.input_snapshot if ex is not None else {}) or {}

        input_snapshot = run_in_short_session(_input_snapshot)
        workflow = SyncWorkflowRepo(username=user_id).get_current_workflow(workflow_id)
        session = await get_sandbox_manager().get_session(
            tenant_id,
            workflow_id,
            user_id=user_id,
            expose_run=True,
        )
        creds = (
            await inject_into_run_context_async(
                {},
                workflow,
                tenant_id,
                user_id=user_id,
                workflow_id=workflow_id,
                execution_id=str(execution_id),
                execution_resource_type=ResourceType.TASK_EXECUTION.value,
                principal_type="service_account",
                principal_id=str(lease.service_account_id),
                principal_generation=lease.generation,
            )
        ).get("llm_credentials")
        stop = asyncio.Event()
        node_events = 0
        workflow_stream = stream_workflow_job(
            stop=stop,
            workflow=workflow,
            inputs=input_snapshot,
            workflow_run_id=workflow_id,
            tenant_id=tenant_id,
            session=session,
            exec_id=str(execution_id),
            timeout=600.0,
            runtime_extra=(
                {"llm_credentials": creds} if creds else None
            ),
            clear_run=True,
        )
        async for msg in workflow_stream:
            if await _execution_cancelled(execution_id):
                stop.set()
                await workflow_stream.aclose()
                final_status = "cancelled"
                error_message = "Execution cancelled."
                break
            mtype = msg.get("type")
            if mtype == "node_event":
                node_events += 1
                node_id = msg.get("node_id")
                status = msg.get("status")
                _emit(task_id, tenant_uuid, "progress", {
                    "schema_version": 1,
                    "level": "info",
                    "category": "scheduled_run",
                    "action": "scheduled_run.node_event",
                    "message": f"Node {node_id or ''} {status or 'updated'}.".strip(),
                    "task_status": "running",
                    "sandbox_status": "running",
                    "scope": {"type": "node", "id": node_id, "name": msg.get("node_name")},
                    "progress": None,
                    "data": {
                        "execution_id": str(execution_id),
                        "schedule_id": str(schedule_id),
                        "node_id": node_id,
                        "node_name": msg.get("node_name"),
                        "status": status,
                        "event_index": node_events,
                    },
                    "error": None,
                })
            elif mtype == "result":
                error_dict = msg.get("error_dict") or {}
                result_payload = {
                    "final_outputs": msg.get("final_outputs") or {},
                    "error_dict": error_dict,
                    "execution_time": msg.get("execution_time"),
                }
                if error_dict:
                    final_status = "failed"
                    error_message = json.dumps(error_dict, ensure_ascii=False, default=str)[:2000]
                break
            elif mtype == "timeout":
                final_status = "failed"
                error_message = msg.get("message") or "Workflow execution timed out."
                break
    except Exception as exc:
        final_status = "failed"
        error_message = str(exc)
        logger.warning("scheduled_run_execution_failed", exc_info=True)

    finished = datetime.now(timezone.utc)
    task_status = "enabled"
    schedule_snapshot = _snapshot_schedule(schedule_id)
    if not schedule_snapshot.get("enabled", True):
        task_status = "paused"
    elif final_status == "failed":
        task_status = "failed"
    _update_execution(
        execution_id,
        status=final_status,
        finished_at=finished,
        result=result_payload,
        error=error_message,
        notification_state=_notification_state(
            schedule_snapshot.get("notification_policy") or {},
            final_status,
        ),
    )
    _update_schedule(
        schedule_id,
        last_run_at=finished,
        last_status=final_status,
    )
    _update_task(
        task_id,
        status=task_status,
        progress=0,
        result=result_payload,
        error=error_message,
        finished_at=finished,
    )
    _emit(task_id, tenant_uuid, "terminal", {
        "schema_version": 1,
        "level": "info" if final_status == "succeeded" else "error",
        "category": "scheduled_run",
        "action": f"scheduled_run.{final_status}",
        "message": (
            "Scheduled run finished."
            if final_status == "succeeded"
            else error_message or f"Scheduled run {final_status}."
        ),
        "task_status": task_status,
        "sandbox_status": "released",
        "scope": {"type": "scheduled_run_execution", "id": str(execution_id), "name": None},
        "progress": None,
        "data": {
            "execution_id": str(execution_id),
            "schedule_id": str(schedule_id),
            "status": final_status,
        },
        "error": (
            {
                "code": "scheduled_run_error",
                "message": error_message,
                "retryable": False,
                "details": {"execution_id": str(execution_id)},
            }
            if error_message and final_status != "succeeded"
            else None
        ),
    })


async def _execution_cancelled(execution_id: uuid.UUID) -> bool:
    async def _runner(session) -> bool:
        ex = await TasksRepo(session).get_scheduled_execution(execution_id)
        return ex is not None and ex.status == "cancelled"

    return await asyncio.to_thread(run_in_short_session, _runner)


def _notification_state(policy: dict, status: str) -> dict:
    wanted = bool(policy.get("enabled", False)) and status in set(policy.get("on") or [])
    if not wanted:
        return {"status": "skipped", "reason": "policy_not_matched"}
    return {
        "status": "queued",
        "channels": list(policy.get("channels") or ["in_app"]),
        "include_detail_link": bool(policy.get("include_detail_link", True)),
    }
