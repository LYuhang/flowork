"""Reconcile dead Task workers without replaying unknown side effects."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, or_, select

from vibecanvas_api.services.sandbox.coordinator import dispose_sandbox_rpc_client
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.task_worker import STALE_SECONDS, WorkerClaim
from vibecanvas_api.storage.models_tasks import ScheduledRunExecution, Task
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.sync_session import short_admin_session

logger = structlog.get_logger(__name__)
UNKNOWN_OUTCOME = (
    "The Task worker was lost. Its isolated sandbox has been stopped, but external "
    "side effects may already have occurred. This attempt was not automatically "
    "rerun. Inspect diagnostics and external results before submitting new work."
)


def recover_task_workers():
    async def run():
        try:
            await reconcile_task_workers()
        finally:
            await dispose_sandbox_rpc_client()
    asyncio.run(run())


def _stale(model, cutoff):
    created = model.submitted_at if model is Task else model.triggered_at
    return or_(model.worker_recovery_pending.is_(True),
               func.coalesce(model.worker_heartbeat_at, model.started_at, created) < cutoff)


async def _fence(kind, resource_id, cutoff):
    model = Task if kind == "batch" else ScheduledRunExecution
    async with short_admin_session() as session:
        row = (await session.execute(select(model).where(model.id == resource_id,
            model.status.in_(("running", "cancelling")), _stale(model, cutoff))
            .with_for_update(skip_locked=True))).scalar_one_or_none()
        if row is None:
            return None
        row.worker_recovery_pending = True
        # Keep the old token to locate its scope across reaper crashes/retries.
        # The pending flag fences all writes and heartbeats from the old worker.
        scope_id = (WorkerClaim(kind, row.id, row.worker_token).scope_id
                    if row.worker_token else f"{kind}-{row.id}")
        return {"tenant_id": str(row.tenant_id), "token": row.worker_token,
                "scope_id": scope_id}


async def _finalize(kind, resource_id, token):
    model = Task if kind == "batch" else ScheduledRunExecution
    async with short_admin_session() as session:
        row = await session.get(model, resource_id, with_for_update=True, populate_existing=True)
        if (row is None or row.worker_token != token or not row.worker_recovery_pending
                or row.status not in {"running", "cancelling"}):
            return False
        repo = TasksRepo(session)
        now = datetime.now(timezone.utc)
        if kind == "batch":
            task = await repo.get(row.id)
            result = dict(task.result or {})
            result.update(can_resume=False, outcome_unknown=True, worker_lost=True,
                          task_status="failed")
            await repo.update_status(row.id, status="failed", result=result,
                                     error=UNKNOWN_OUTCOME, finished_at=now)
            task_id, task_status = row.id, "failed"
        else:
            schedule = await repo.get_schedule(row.schedule_id)
            if schedule is None:
                return False
            status = "cancelled" if row.status == "cancelling" else "failed"
            await repo.update_scheduled_execution(row.id, status=status, finished_at=now,
                error=UNKNOWN_OUTCOME, result={"outcome_unknown": True, "worker_lost": True})
            await repo.update_schedule(schedule.id, last_run_at=now, last_status=status)
            task_id = schedule.task_id
            task_status = "failed" if schedule.enabled else "paused"
            await repo.update_status(task_id, status=task_status, error=UNKNOWN_OUTCOME,
                finished_at=now, result={"outcome_unknown": True, "worker_lost": True})
        row.worker_recovery_pending = False
        # Revocation persists after recovery, so a delayed old write cannot
        # mutate the terminal state, even after the heartbeat sweep is done.
        row.worker_token = None
        await repo.insert_event(task_id, "terminal", {
            "schema_version": 1, "level": "error", "category": "task",
            "action": "task.worker_lost", "message": UNKNOWN_OUTCOME,
            "task_status": task_status, "sandbox_status": "released",
            "scope": {"type": "task" if kind == "batch" else "scheduled_run_execution",
                      "id": str(resource_id), "name": None},
            "progress": None,
            "data": {"execution_id": str(resource_id)} if kind == "schedule" else {},
            "error": {"code": "worker_lost", "message": UNKNOWN_OUTCOME,
                      "retryable": False, "details": {"outcome_unknown": True}},
        }, row.tenant_id)
        return True


async def reconcile_task_workers(limit=50):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS)
    manager = get_sandbox_manager()
    for kind, model in (("batch", Task), ("schedule", ScheduledRunExecution)):
        async with short_admin_session() as session:
            query = select(model.id).where(model.status.in_(("running", "cancelling")), _stale(model, cutoff))
            if kind == "batch":
                query = query.where(Task.task_type == "batch_exec")
            ids = list((await session.execute(query.order_by(model.worker_heartbeat_at.asc().nullsfirst()).limit(limit))).scalars())
        for resource_id in ids:
            try:
                fenced = await _fence(kind, resource_id, cutoff)
                if fenced is None:
                    continue
                reply = await manager.terminate_task_scope(fenced["tenant_id"], fenced["scope_id"])
                if not isinstance(reply, dict) or reply.get("stopped") is not True:
                    raise RuntimeError("Task sandbox shutdown was not confirmed.")
                await _finalize(kind, resource_id, fenced["token"])
            except Exception:
                # Leave the durable recovery flag set. Retry only cleanup, not
                # workflow execution, on the next scheduled reconciliation.
                logger.warning("task_worker_recovery_pending", kind=kind, resource_id=str(resource_id), exc_info=True)
