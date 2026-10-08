"""Background tasks for user-facing scheduled workflow runs."""
from __future__ import annotations

from vibecanvas_api.services.sandbox.contracts import TaskRunSource

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from vibecanvas_api.authorization.types import ResourceType
from vibecanvas_api.services.background_queue import (
    enqueue_background_job_in_transaction,
)
from vibecanvas_api.services.llm_credentials_inject import inject_into_run_context_async
from vibecanvas_api.services.queue_routing import route_for
from vibecanvas_api.services.sandbox.coordinator import (
    dispose_sandbox_rpc_client,
)
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.scheduled_runs import compute_next_run_at, utc_now
from vibecanvas_api.services.workflow_sandbox_runner import prepare_code_pythonpath
from vibecanvas_api.services.workflow_execution_history import create_execution
from vibecanvas_api.services.workflow_history_runner import execute_history_workflow
from vibecanvas_api.services.task_worker import (
    WorkerOwnershipLost, assert_worker_owner, claim_worker, current_claim, watch_worker,
)
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.models_tasks import (
    TaskSchedule,
)
from vibecanvas_api.storage.repo_service_accounts import (
    ServiceAccountLease,
    ServiceAccountsRepo,
)
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from vibecanvas_api.storage.db import dispose_engine, session_scope
from vibecanvas_api.storage.sync_session import (
    current_sync_tenant_id,
    short_admin_session,
)

logger = structlog.get_logger(__name__)
SCHEDULED_RUN_DISPATCH_INTERVAL_SEC = 60.0


async def _emit(task_id: uuid.UUID, tenant_id: uuid.UUID, event_type: str, payload: dict) -> None:
    async with session_scope(tenant_id=current_sync_tenant_id.get()) as session:
        await assert_worker_owner(session)
        repo = TasksRepo(session)
        task = await repo.get(task_id)
        if task is not None:
            payload["task_status"] = task.status
        await repo.insert_event(task_id, event_type, payload, tenant_id)


async def _refresh_task(task_id: uuid.UUID) -> None:
    async with session_scope(tenant_id=current_sync_tenant_id.get()) as session:
        repo = TasksRepo(session)
        # Control routes lock schedule before execution; workers use the same order.
        await repo.get_schedule_by_task(task_id, for_update=True)
        await assert_worker_owner(session)
        await repo.refresh_scheduled_task(task_id)


async def _update_execution(execution_id: uuid.UUID, **fields: object) -> None:
    async with session_scope(tenant_id=current_sync_tenant_id.get()) as session:
        await assert_worker_owner(session)
        await TasksRepo(session).update_scheduled_execution(execution_id, **fields)


async def _snapshot_schedule(schedule_id: uuid.UUID) -> dict:
    async with session_scope(tenant_id=current_sync_tenant_id.get()) as session:
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


async def _scheduled_execution_lease(
    *,
    task_id: uuid.UUID,
    schedule_id: uuid.UUID,
    workflow_id: str,
) -> ServiceAccountLease:
    async with session_scope(tenant_id=current_sync_tenant_id.get()) as session:
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
            next_run = None if schedule.schedule_type == "once" else compute_next_run_at(
                schedule_type=schedule.schedule_type,
                timezone_name=schedule.timezone,
                interval_seconds=schedule.interval_seconds,
                cron_expr=schedule.cron_expr,
                base=now,
            )
            if schedule.schedule_type == "interval":
                # Skip missed slots without shifting the original cadence.
                interval = timedelta(seconds=schedule.interval_seconds)
                elapsed = max(0, (now - schedule.next_run_at) // interval)
                next_run = schedule.next_run_at + (elapsed + 1) * interval
            run_key = f"{schedule.id}:{schedule.next_run_at.isoformat()}"
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
            if inserted is not None:
                await enqueue_background_job_in_transaction(
                    session,
                    "scheduled_runs.execute",
                    job_id=str(execution_id),
                    queue=route_for("scheduled_run"),
                    kwargs={"execution_id": str(execution_id)},
                )
            await repo.refresh_scheduled_task(schedule.task_id)


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
    **kwargs,
) -> None:
    claim = await _claim_execution(kwargs["execution_id"])
    if claim is None:
        return
    context = current_claim.set(claim)
    try:
        await _execute_owned_scheduled_run(**kwargs)
    except WorkerOwnershipLost:
        logger.warning("scheduled_run_worker_ownership_lost", execution_id=str(kwargs["execution_id"]))
    finally:
        current_claim.reset(context)


async def _execute_owned_scheduled_run(
    *,
    task_id: uuid.UUID,
    schedule_id: uuid.UUID,
    execution_id: uuid.UUID,
    tenant_id: str,
    user_id: str,
    workflow_id: str,
) -> None:
    tenant_uuid = uuid.UUID(tenant_id)
    try:
        lease = await _scheduled_execution_lease(
            task_id=task_id,
            schedule_id=schedule_id,
            workflow_id=workflow_id,
        )
    except LookupError:
        finished = datetime.now(timezone.utc)
        await _update_execution(
            execution_id,
            status="failed",
            finished_at=finished,
            error="service_account_unavailable",
        )
        await _refresh_task(task_id)
        await _emit(task_id, tenant_uuid, "terminal", {
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
    await _refresh_task(task_id)
    await _emit(task_id, tenant_uuid, "state", {
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

    # A vanished sandbox / exhausted stream is not evidence of success.
    final_status = "failed"
    result_payload: dict | None = None
    error_message: str | None = "Execution ended without a result."
    stop = asyncio.Event()
    watcher = asyncio.create_task(_watch_cancellation(execution_id, stop))
    claim = current_claim.get()
    ownership_watcher = asyncio.create_task(watch_worker(claim, stop)) if claim else None
    execution_scope = claim.scope_id if claim else f"schedule-{execution_id}"
    manager = get_sandbox_manager()
    session = None
    try:
        input_snapshot, workflow, workflow_version, mount_enabled = await _execution_inputs(
            execution_id, schedule_id, workflow_id, user_id)
        from vibecanvas_api.services.service_account_resources import refresh_scheduled_resources
        await refresh_scheduled_resources(tenant_id=tenant_id, user_id=user_id,
            workflow_id=workflow_id, execution_id=str(execution_id),
            service_account_id=str(lease.service_account_id), generation=lease.generation,
            workflow=workflow)
        session = await manager.get_session(
            tenant_id,
            execution_scope,
            task_run_source=TaskRunSource(task_id=task_id),
            user_id=user_id,
            expose_run=True,
            expose_mount=mount_enabled,
            lease="resident",  # Protect preparation, approval waits and RPC gaps until finally closes it.
        )
        runtime_extra = (
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
        )
        from vibecanvas_api.services.workflow_resources import prepare_execution_resources
        resources = await prepare_execution_resources(
            sandbox_session=session, workflow=workflow, tenant_id=tenant_id,
            user_id=user_id, workflow_id=workflow_id, execution_id=str(execution_id),
            execution_resource_type=ResourceType.TASK_EXECUTION.value,
            principal_type="service_account", principal_id=str(lease.service_account_id),
            principal_generation=lease.generation,
        )
        if resources:
            runtime_extra["workflow_resources"] = resources
        code_pythonpath = await prepare_code_pythonpath(workflow, session=session)
        if code_pythonpath:
            runtime_extra["code_pythonpath"] = code_pythonpath
        history_id = await create_execution(
            execution_id=str(execution_id), tenant_id=tenant_id, source_type="task", source_id=str(task_id),
            user_id=user_id, workflow_id=workflow_id, workflow=workflow, inputs=input_snapshot,
            workflow_version=workflow_version,
        )
        node_events = 0
        resource_audits = {}

        async def on_state(state):
            await asyncio.to_thread(_emit, task_id, tenant_uuid, "progress", {
                "schema_version": 1, "level": "info", "category": "scheduled_run",
                "action": f"scheduled_run.{state}", "message": f"Scheduled execution: {state}.",
                "task_status": "running", "sandbox_status": "running",
                "scope": {"type": "scheduled_run_execution", "id": history_id, "name": None},
                "progress": None,
                "data": {"execution_id": history_id, "schedule_id": str(schedule_id), "status": state,
                         "execution_url": f"/workflow-executions/{history_id}"},
                "error": None,
            })

        async def on_event(msg):
            nonlocal node_events
            if msg.get("type") == "node_event":
                node_events += 1
                node_id = msg.get("node_id")
                status = msg.get("status")
                if node_id and isinstance(msg.get('resource_audit'), dict):
                    resource_audits[node_id] = msg['resource_audit']
                await asyncio.to_thread(_emit, task_id, tenant_uuid, "progress", {
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
                        **({'resource_audit': msg['resource_audit']} if isinstance(msg.get('resource_audit'), dict) else {}),
                    },
                    "error": None,
                })
        response = await execute_history_workflow(
            session=session, tenant_id=tenant_id, execution_id=history_id, workflow=workflow,
            inputs=input_snapshot, context=runtime_extra, stop=stop, on_state=on_state, on_event=on_event,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Execution ended without a result.")
        error_dict = result.get("error_dict") or {}
        final_status = "failed" if error_dict else "succeeded"
        error_message = json.dumps(error_dict, ensure_ascii=False, default=str)[:2000] if error_dict else None
        if (response.get("status") or {}).get("status") == "cancelled":
            final_status = "cancelled"
            error_message = "Execution cancelled."
        result_payload = {
            **result, **({'resource_audits': resource_audits} if resource_audits else {}),
            "execution_id": history_id, "execution_url": f"/workflow-executions/{history_id}",
        }
    except Exception as exc:
        final_status = "failed"
        error_message = str(exc)
        logger.warning("scheduled_run_execution_failed", exc_info=True)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if ownership_watcher is not None:
            ownership_watcher.cancel()
            ownership_result = (await asyncio.gather(ownership_watcher, return_exceptions=True))[0]
        if session is not None:
            try:
                await manager.close_session(tenant_id, execution_scope)
            except Exception:
                logger.warning("scheduled_run_sandbox_cleanup_failed", exc_info=True)
        if ownership_watcher is not None and isinstance(ownership_result, WorkerOwnershipLost):
            raise ownership_result
    if stop.is_set() or await _execution_cancelled(execution_id):
        final_status = "cancelled"
        error_message = "Execution cancelled."

    finished = datetime.now(timezone.utc)
    schedule_snapshot = await _snapshot_schedule(schedule_id)
    await _update_execution(
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
    await _refresh_task(task_id)
    await _emit(task_id, tenant_uuid, "terminal", {
        "schema_version": 1,
        "level": "info" if final_status == "succeeded" else "error",
        "category": "scheduled_run",
        "action": f"scheduled_run.{final_status}",
        "message": (
            "Scheduled run finished."
            if final_status == "succeeded"
            else error_message or f"Scheduled run {final_status}."
        ),
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


async def _claim_execution(execution_id: uuid.UUID):
    """Serialize worker startup against cancellation and duplicate delivery."""
    async with session_scope(tenant_id=current_sync_tenant_id.get()) as session:
        return await claim_worker(session, "schedule", execution_id)


async def _watch_cancellation(execution_id: uuid.UUID, stop: asyncio.Event) -> None:
    from vibecanvas_api.services.state_notifications import state_changes
    async with state_changes('flowork_schedule_command', str(execution_id)) as changed:
        while not stop.is_set():
            changed.clear()
            if await _execution_cancelled(execution_id):
                stop.set()
                return
            await changed.wait()


async def _execution_cancelled(execution_id: uuid.UUID) -> bool:
    async with session_scope(tenant_id=current_sync_tenant_id.get()) as session:
        ex = await TasksRepo(session).get_scheduled_execution(execution_id)
        return ex is not None and ex.status in {"cancelled", "cancelling"}


def _notification_state(policy: dict, status: str) -> dict:
    from vibecanvas_api.services.task_notifications import notification_state
    return notification_state(policy, status)


async def _execution_inputs(execution_id, schedule_id, workflow_id, user_id) -> tuple:
    async with session_scope(tenant_id=current_sync_tenant_id.get()) as session:
        await assert_worker_owner(session)
        repo = TasksRepo(session)
        ex = await repo.get_scheduled_execution(execution_id)
        frozen = getattr(ex, "workflow_snapshot", None) or {}
        schedule = await repo.get_schedule(schedule_id)
        workflow = frozen.get("workflow")
        if workflow is None:  # Legacy execution queued before snapshot support.
            workflow = await WorkflowRepo(session, user_id).get_current_workflow(workflow_id)
        return ((ex.input_snapshot if ex is not None else {}) or {}, workflow,
                frozen.get("version"),
                frozen.get("mount_enabled", bool(schedule and schedule.mount_enabled)))
