"""DBOS workflow and step registrations for Flowork background work.

Business implementations live in ``background_tasks`` and remain independent
of DBOS. Each durable workflow contains one explicit side-effecting step. DBOS
arguments contain opaque business-record ids only; adapters load encrypted
inputs from Flowork storage immediately before calling the implementation.
The business tables keep user-visible progress and idempotency; DBOS owns
delivery, recovery, queue flow control, and periodic scheduling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable
import uuid

from dbos import DBOS
from sqlalchemy import select

from vibecanvas_api.background_tasks.authorization_reconciler import (
    reconcile_authorization,
)
from vibecanvas_api.background_tasks.authorization_apply import (
    apply_authorization_mutation,
)
from vibecanvas_api.background_tasks.batch_exec import batch_exec
from vibecanvas_api.background_tasks.concurrency_reconciler import (
    reconcile_concurrency,
)
from vibecanvas_api.background_tasks.data_purge import run_due
from vibecanvas_api.background_tasks.deployment_invoke import deployment_invoke
from vibecanvas_api.background_tasks.invoke_counter_flush import flush_invoke_counters
from vibecanvas_api.background_tasks.kb_gc_sweeper import kb_gc_sweeper
from vibecanvas_api.background_tasks.kb_indexer import kb_index_file_task
from vibecanvas_api.background_tasks.kb_orphan_reconciler import (
    kb_orphan_reconciler,
)
from vibecanvas_api.background_tasks.reconciler import resubmit_stuck_queued
from vibecanvas_api.background_tasks.scheduled_runs import (
    dispatch_due_scheduled_runs,
    execute_scheduled_run,
)
from vibecanvas_api.storage.models_kb import KbFile
from vibecanvas_api.storage.models_tasks import Task, TaskSchedule
from vibecanvas_api.storage.repo_deployment_invocations import (
    DeploymentInvocationsRepo,
)
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.sync_session import short_admin_session


async def _load_batch_payload(task_id: str) -> dict[str, Any]:
    async with short_admin_session() as session:
        task = await session.get(Task, uuid.UUID(task_id))
        if task is None:
            raise LookupError(f"background task {task_id} not found")
        await TasksRepo(session).materialize_task(task)
        payload = task.payload or {}
        if not task.workflow_id:
            raise ValueError(f"background task {task_id} has no workflow")
        return {
            "task_id": str(task.id),
            "tenant_id": str(task.tenant_id),
            "user_id": str(task.user_id),
            "workflow_id": task.workflow_id,
            "data_source": payload.get("data_source", {}),
            "column_mapping": payload.get("column_mapping", {}),
            "output": payload.get("output"),
            "output_columns": payload.get("output_columns"),
            "concurrency": payload.get("concurrency", 1),
            "resume": task.status == "resuming",
        }


def _run_batch_from_id(*, task_id: str) -> Any:
    return batch_exec(**asyncio.run(_load_batch_payload(task_id)))


async def _load_deployment_payload(invocation_id: str) -> dict[str, Any]:
    async with short_admin_session() as session:
        payload = await DeploymentInvocationsRepo(session).load_worker_payload(
            uuid.UUID(invocation_id)
        )
        if payload is None:
            raise LookupError(f"deployment invocation {invocation_id} not found")
        return payload


def _run_deployment_from_id(*, invocation_id: str) -> Any:
    return deployment_invoke(
        **asyncio.run(_load_deployment_payload(invocation_id))
    )


async def _load_kb_payload(file_id: str) -> dict[str, Any]:
    async with short_admin_session() as session:
        row = (
            await session.execute(
                select(KbFile).where(KbFile.id == uuid.UUID(file_id))
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"knowledge-base file {file_id} not found")
        return {
            "task_id": file_id,
            "tenant_id": str(row.tenant_id),
            "file_id": str(row.id),
            "user_id": str(row.user_id),
        }


def _run_kb_from_id(*, file_id: str) -> Any:
    return kb_index_file_task(**asyncio.run(_load_kb_payload(file_id)))


async def _load_scheduled_payload(execution_id: str) -> dict[str, Any]:
    async with short_admin_session() as session:
        repo = TasksRepo(session)
        execution = await repo.get_scheduled_execution(uuid.UUID(execution_id))
        if execution is None:
            raise LookupError(f"scheduled execution {execution_id} not found")
        schedule = await session.get(TaskSchedule, execution.schedule_id)
        if schedule is None:
            raise LookupError(
                f"task schedule {execution.schedule_id} not found"
            )
        return {
            "task_id": str(schedule.task_id),
            "schedule_id": str(schedule.id),
            "execution_id": str(execution.id),
            "tenant_id": str(execution.tenant_id),
            "user_id": str(schedule.user_id),
            "workflow_id": execution.workflow_id,
        }


def _run_scheduled_from_id(*, execution_id: str) -> Any:
    return execute_scheduled_run(
        **asyncio.run(_load_scheduled_payload(execution_id))
    )


def _task_registration(
    workflow_name: str,
    implementation: Callable[..., Any],
    *,
    retries_allowed: bool = False,
    max_attempts: int = 3,
):
    @DBOS.step(
        name=f"{workflow_name}.step",
        retries_allowed=retries_allowed,
        interval_seconds=1.0,
        max_attempts=max_attempts,
        backoff_rate=2.0,
    )
    def task_step(payload: dict[str, Any]) -> Any:
        return implementation(**payload)

    @DBOS.workflow(name=workflow_name, max_recovery_attempts=5)
    def task_workflow(payload: dict[str, Any]) -> Any:
        return task_step(payload)

    return task_workflow


batch_exec_workflow = _task_registration("batch_exec", _run_batch_from_id)
deployment_invoke_workflow = _task_registration(
    "deployment_invoke", _run_deployment_from_id
)
kb_index_file_workflow = _task_registration("kb.index_file", _run_kb_from_id)
scheduled_run_workflow = _task_registration(
    "scheduled_runs.execute", _run_scheduled_from_id
)
authorization_apply_workflow = _task_registration(
    "authorization.apply_mutation",
    apply_authorization_mutation,
    retries_allowed=True,
    max_attempts=12,
)

TASK_WORKFLOWS = {
    "batch_exec": batch_exec_workflow,
    "deployment_invoke": deployment_invoke_workflow,
    "kb.index_file": kb_index_file_workflow,
    "scheduled_runs.execute": scheduled_run_workflow,
    "authorization.apply_mutation": authorization_apply_workflow,
}


def _schedule_registration(
    workflow_name: str,
    implementation: Callable[[], Any],
):
    @DBOS.step(name=f"{workflow_name}.step", retries_allowed=False)
    def schedule_step() -> Any:
        return implementation()

    @DBOS.workflow(name=workflow_name, max_recovery_attempts=5)
    def schedule_workflow(
        scheduled_time: datetime,  # noqa: ARG001 - part of DBOS schedule contract
        context: Any,  # noqa: ARG001 - reserved for future schedule metadata
    ) -> Any:
        return schedule_step()

    return schedule_workflow


dispatch_due_workflow = _schedule_registration(
    "scheduled_runs.dispatch_due", dispatch_due_scheduled_runs
)
queued_reconciler_workflow = _schedule_registration(
    "background.reconcile_queued", resubmit_stuck_queued
)
authorization_reconciler_workflow = _schedule_registration(
    "authorization.audit", reconcile_authorization
)
data_purge_workflow = _schedule_registration("data_purge.run_due", run_due)
concurrency_reconciler_workflow = _schedule_registration(
    "deployments.concurrency_reconciler", reconcile_concurrency
)
invoke_counter_flush_workflow = _schedule_registration(
    "deployments.flush_invoke_counters", flush_invoke_counters
)
kb_gc_workflow = _schedule_registration("kb.gc_sweeper", kb_gc_sweeper)
kb_orphan_workflow = _schedule_registration(
    "kb.orphan_reconciler", kb_orphan_reconciler
)


SCHEDULES = [
    {
        "schedule_name": "flowork-data-purge",
        "workflow_fn": data_purge_workflow,
        # DBOS cron has a leading seconds field.  Keep this low-frequency:
        # `*/5 * * * * *` means every five seconds, not every five minutes.
        "schedule": "0 */5 * * * *",
        "queue_name": "maintenance",
    },
    {
        "schedule_name": "flowork-queued-reconciler",
        "workflow_fn": queued_reconciler_workflow,
        # Delivery is event-driven; this sweep only repairs the exceptional
        # request-committed/DBOS-enqueue-failed gap.
        "schedule": "0 */5 * * * *",
        "queue_name": "control",
    },
    {
        "schedule_name": "flowork-authorization-audit",
        "workflow_fn": authorization_reconciler_workflow,
        "schedule": "0 17 3 * * *",
        "queue_name": "control",
    },
    {
        "schedule_name": "flowork-scheduled-run-dispatcher",
        "workflow_fn": dispatch_due_workflow,
        "schedule": "0 * * * * *",
        "queue_name": "control",
    },
    {
        "schedule_name": "flowork-invoke-counter-flush",
        "workflow_fn": invoke_counter_flush_workflow,
        "schedule": "0 * * * * *",
        "queue_name": "control",
    },
    {
        "schedule_name": "flowork-kb-orphan-reconciler",
        "workflow_fn": kb_orphan_workflow,
        "schedule": "0 */5 * * * *",
        "queue_name": "maintenance",
    },
    {
        "schedule_name": "flowork-kb-gc",
        "workflow_fn": kb_gc_workflow,
        "schedule": "0 0 */6 * * *",
        "queue_name": "maintenance",
    },
    {
        "schedule_name": "flowork-deployment-concurrency-reconciler",
        "workflow_fn": concurrency_reconciler_workflow,
        "schedule": "0 0 0 * * *",
        "queue_name": "control",
    },
]


SCHEDULE_WORKFLOWS = {
    "scheduled_runs.dispatch_due": dispatch_due_workflow,
    "background.reconcile_queued": queued_reconciler_workflow,
    "authorization.audit": authorization_reconciler_workflow,
    "data_purge.run_due": data_purge_workflow,
    "deployments.concurrency_reconciler": concurrency_reconciler_workflow,
    "deployments.flush_invoke_counters": invoke_counter_flush_workflow,
    "kb.gc_sweeper": kb_gc_workflow,
    "kb.orphan_reconciler": kb_orphan_workflow,
}


RETIRED_SCHEDULES = ("flowork-authorization-reconciler",)


__all__ = [
    "RETIRED_SCHEDULES",
    "SCHEDULES",
    "SCHEDULE_WORKFLOWS",
    "TASK_WORKFLOWS",
]
