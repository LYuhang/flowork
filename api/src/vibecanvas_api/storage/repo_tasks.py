"""CRUD for ``tasks`` and ``task_events``.

All methods assume the session is already tenant-bound — either via the
async DI ``session_scope(tenant_id=...)`` (request path) or via
``run_in_short_session`` with ``current_sync_tenant_id`` set (worker
path). FORCE RLS on ``tasks`` / ``task_events`` (migration 004) enforces
cross-tenant isolation regardless.

The background worker writes via the async path (``session_scope`` driven
from ``asyncio.run`` inside the task body) so this repo stays a pure
async API — no sync facade needed at T9. Routes (T10/T11/T12) use the
same async ``TasksRepo`` directly through their DI session.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import String, cast, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import (
    ContentCiphertext,
    content_encryption_service,
)
from vibecanvas_api.storage.models_tasks import (
    ScheduledRunExecution,
    Task,
    TaskEvent,
    TaskSchedule,
)


# Whitelist of ``Task`` columns that ``update_status`` is allowed to set.
# Using a fixed allowlist instead of free-form SQL string concat keeps
# the update path immune to caller-controlled column names (defence in
# depth — callers in this codebase are all internal, but the allowlist
# also doubles as a schema-evolution checklist).
_TASK_UPDATABLE_FIELDS = frozenset({
    "status", "progress", "payload", "result", "results_uri", "error",
    "background_job_id", "started_at", "finished_at",
})

TASK_EVENT_TYPES = frozenset({"state", "progress", "log", "result", "terminal"})
_TASK_PRIVATE_FIELDS = frozenset({"payload", "result", "error"})
_SCHEDULE_PRIVATE_FIELDS = frozenset({
    "name",
    "input_preset",
    "notification_policy",
})
_EXECUTION_PRIVATE_FIELDS = frozenset({
    "input_snapshot", "result", "error", "run_state", "notification_state",
})


def _normalized_task_ids(
    task_ids: tuple[str, ...] | list[str] | None,
) -> tuple[uuid.UUID, ...] | None:
    if task_ids is None:
        return None
    normalized: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for value in task_ids:
        try:
            parsed = uuid.UUID(str(value))
        except (TypeError, ValueError):
            continue
        if parsed not in seen:
            seen.add(parsed)
            normalized.append(parsed)
    return tuple(normalized)


class TasksRepo:
    """Async CRUD over ``tasks`` + ``task_events``.

    Caller owns the transaction: each public method ``flush()``-es but
    does NOT ``commit()``. The DI request session commits at request end;
    background writers (the background worker task body) commit explicitly via the
    ``session_scope`` context manager.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _encrypt_document(
        self,
        *,
        tenant_id: uuid.UUID,
        resource_type: str,
        resource_id: str,
        purpose: str,
        record_id: str,
        value: dict,
    ) -> ContentCiphertext:
        return await content_encryption_service().encrypt_json(
            self.session,
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            purpose=purpose,
            record_id=record_id,
            value=value,
        )

    async def _decrypt_document(
        self,
        *,
        tenant_id: uuid.UUID,
        resource_type: str,
        resource_id: str,
        purpose: str,
        record_id: str,
        key_id: uuid.UUID,
        ciphertext: str,
        nonce: str,
    ) -> dict:
        value = await content_encryption_service().decrypt_json(
            self.session,
            key_id=key_id,
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            purpose=purpose,
            record_id=record_id,
            ciphertext=ciphertext,
            nonce=nonce,
        )
        if not isinstance(value, dict):
            raise ValueError(f"{purpose} ciphertext must contain an object")
        return value

    async def _materialize_task(self, task: Task) -> Task:
        value = await self._decrypt_document(
            tenant_id=task.tenant_id,
            resource_type="task",
            resource_id=str(task.id),
            purpose="task_private",
            record_id=str(task.id),
            key_id=task.content_key_id,
            ciphertext=task.content_ciphertext,
            nonce=task.content_nonce,
        )
        task.payload = value.get("payload") or {}
        task.result = value.get("result")
        task.error = value.get("error")
        return task

    async def materialize_task(self, task: Task) -> Task:
        """Decrypt one already-authorized/admin-selected Task projection."""
        return await self._materialize_task(task)

    async def _store_task_private(self, task: Task, value: dict) -> None:
        encrypted = await self._encrypt_document(
            tenant_id=task.tenant_id,
            resource_type="task",
            resource_id=str(task.id),
            purpose="task_private",
            record_id=str(task.id),
            value=value,
        )
        task.content_ciphertext = encrypted.ciphertext
        task.content_nonce = encrypted.nonce
        task.content_key_id = encrypted.key_id
        task.payload = value.get("payload") or {}
        task.result = value.get("result")
        task.error = value.get("error")

    async def _materialize_schedule(self, schedule: TaskSchedule) -> TaskSchedule:
        value = await self._decrypt_document(
            tenant_id=schedule.tenant_id,
            resource_type="task",
            resource_id=str(schedule.task_id),
            purpose="task_schedule_private",
            record_id=str(schedule.id),
            key_id=schedule.private_key_id,
            ciphertext=schedule.private_ciphertext,
            nonce=schedule.private_nonce,
        )
        schedule.name = str(value.get("name") or "")
        schedule.input_preset = value.get("input_preset") or {}
        schedule.notification_policy = value.get("notification_policy") or {}
        return schedule

    async def _store_schedule_private(
        self,
        schedule: TaskSchedule,
        value: dict,
    ) -> None:
        encrypted = await self._encrypt_document(
            tenant_id=schedule.tenant_id,
            resource_type="task",
            resource_id=str(schedule.task_id),
            purpose="task_schedule_private",
            record_id=str(schedule.id),
            value=value,
        )
        schedule.private_ciphertext = encrypted.ciphertext
        schedule.private_nonce = encrypted.nonce
        schedule.private_key_id = encrypted.key_id
        schedule.private_schema_version = 2
        schedule.name = str(value.get("name") or "")
        schedule.input_preset = value.get("input_preset") or {}
        schedule.notification_policy = value.get("notification_policy") or {}

    async def _materialize_execution(
        self,
        execution: ScheduledRunExecution,
    ) -> ScheduledRunExecution:
        value = await self._decrypt_document(
            tenant_id=execution.tenant_id,
            resource_type="task_execution",
            resource_id=str(execution.id),
            purpose="scheduled_execution_private",
            record_id=str(execution.id),
            key_id=execution.private_key_id,
            ciphertext=execution.private_ciphertext,
            nonce=execution.private_nonce,
        )
        execution.input_snapshot = value.get("input_snapshot") or {}
        execution.result = value.get("result")
        execution.error = value.get("error")
        execution.run_state = value.get("run_state") or {}
        execution.notification_state = value.get("notification_state") or {}
        return execution

    async def _store_execution_private(
        self,
        execution: ScheduledRunExecution,
        value: dict,
    ) -> None:
        encrypted = await self._encrypt_document(
            tenant_id=execution.tenant_id,
            resource_type="task_execution",
            resource_id=str(execution.id),
            purpose="scheduled_execution_private",
            record_id=str(execution.id),
            value=value,
        )
        execution.private_ciphertext = encrypted.ciphertext
        execution.private_nonce = encrypted.nonce
        execution.private_key_id = encrypted.key_id
        execution.input_snapshot = value.get("input_snapshot") or {}
        execution.result = value.get("result")
        execution.error = value.get("error")
        execution.run_state = value.get("run_state") or {}
        execution.notification_state = value.get("notification_state") or {}

    async def create(
        self,
        *,
        task_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        workflow_id: str | None,
        task_type: str,
        payload: dict,
        background_job_id: str | None = None,
        deployment_id: uuid.UUID | None = None,
        service_account_id: uuid.UUID | None = None,
    ) -> Task:
        encrypted = await self._encrypt_document(
            tenant_id=tenant_id,
            resource_type="task",
            resource_id=str(task_id),
            purpose="task_private",
            record_id=str(task_id),
            value={"payload": payload, "result": None, "error": None},
        )
        task = Task(
            id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            owner_id=user_id,
            workflow_id=workflow_id,
            task_type=task_type,
            content_ciphertext=encrypted.ciphertext,
            content_nonce=encrypted.nonce,
            content_key_id=encrypted.key_id,
            background_job_id=background_job_id,
            deployment_id=deployment_id,
            service_account_id=service_account_id,
        )
        self.session.add(task)
        await self.session.flush()
        task.payload = payload
        task.result = None
        task.error = None
        return task

    async def get(self, task_id: uuid.UUID) -> Optional[Task]:
        task = await self.session.get(Task, task_id)
        return await self._materialize_task(task) if task is not None else None

    async def update_status(self, task_id: uuid.UUID, **fields: Any) -> None:
        """Update one-or-more whitelisted columns on a ``Task`` row.

        Unknown column names raise ``ValueError`` — this keeps the
        background worker task body honest about which fields it touches and
        catches typos at the repo boundary, not via a silent no-op.
        """
        if not fields:
            return
        unknown = set(fields) - _TASK_UPDATABLE_FIELDS
        if unknown:
            raise ValueError(
                f"TasksRepo.update_status: unknown columns {sorted(unknown)}; "
                f"allowed = {sorted(_TASK_UPDATABLE_FIELDS)}"
            )
        # Use the ORM update so column types (JSONB, timestamptz, etc.)
        # are bound correctly — no manual SQL string concatenation.
        task = await self.session.get(Task, task_id)
        if task is None:
            raise LookupError(f"Task {task_id} not found")
        private_updates = _TASK_PRIVATE_FIELDS.intersection(fields)
        if private_updates:
            await self._materialize_task(task)
            private = {
                "payload": task.payload,
                "result": task.result,
                "error": task.error,
            }
            private.update({key: fields[key] for key in private_updates})
            await self._store_task_private(task, private)
        for k, v in fields.items():
            if k in _TASK_PRIVATE_FIELDS:
                continue
            setattr(task, k, v)
        await self.session.flush()

    async def insert_event(
        self,
        task_id: uuid.UUID,
        event_type: str,
        payload: dict,
        tenant_id: uuid.UUID,
    ) -> int:
        if event_type not in TASK_EVENT_TYPES:
            raise ValueError(
                f"TasksRepo.insert_event: unknown event_type {event_type!r}; "
                f"allowed = {sorted(TASK_EVENT_TYPES)}"
            )
        record_id = uuid.uuid4()
        encrypted = await self._encrypt_document(
            tenant_id=tenant_id,
            resource_type="task",
            resource_id=str(task_id),
            purpose="task_event",
            record_id=str(record_id),
            value=payload,
        )
        ev = TaskEvent(
            task_id=task_id,
            event_type=event_type,
            tenant_id=tenant_id,
            encryption_record_id=record_id,
            payload_ciphertext=encrypted.ciphertext,
            payload_nonce=encrypted.nonce,
            payload_key_id=encrypted.key_id,
        )
        self.session.add(ev)
        await self.session.flush()
        ev.payload = payload
        return ev.id

    async def list_for_tenant(
        self,
        *,
        task_ids: tuple[str, ...] | list[str] | None = None,
        status: list[str] | None = None,
        task_type: list[str] | None = None,
        workflow_id: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        """List tasks visible to the current tenant (RLS-scoped).

        Ordered newest-first by ``submitted_at``. The ``ix_tasks_tenant_status``
        index covers the common (tenant, status, submitted_at DESC) page.
        """
        normalized_ids = _normalized_task_ids(task_ids)
        if task_ids is not None and not normalized_ids:
            return [], 0
        stmt = select(Task)
        if normalized_ids is not None:
            stmt = stmt.where(Task.id.in_(normalized_ids))
        if status:
            stmt = stmt.where(Task.status.in_(status))
        if task_type:
            stmt = stmt.where(Task.task_type.in_(task_type))
        if workflow_id:
            stmt = stmt.where(Task.workflow_id == workflow_id)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(or_(
                cast(Task.id, String).ilike(pattern),
                Task.workflow_id.ilike(pattern),
                Task.results_uri.ilike(pattern),
            ))
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        stmt = stmt.order_by(Task.submitted_at.desc()).limit(limit).offset(offset)
        rows_result = await self.session.execute(stmt)
        count_result = await self.session.execute(count_stmt)
        rows = list(rows_result.scalars().all())
        for row in rows:
            await self._materialize_task(row)
        return rows, int(count_result.scalar_one())

    async def summary_for_tenant(
        self,
        *,
        task_ids: tuple[str, ...] | list[str] | None = None,
        task_type: list[str] | None = None,
    ) -> dict[str, int]:
        normalized_ids = _normalized_task_ids(task_ids)
        stmt = select(Task.status, func.count()).group_by(Task.status)
        if normalized_ids is not None:
            stmt = stmt.where(Task.id.in_(normalized_ids))
        if task_type:
            stmt = stmt.where(Task.task_type.in_(task_type))
        result = await self.session.execute(stmt)
        counts = {str(status): int(count) for status, count in result.all()}
        active = sum(counts.get(s, 0) for s in ("queued", "running", "cancelling", "resuming"))
        return {
            "active": active,
            "enabled": counts.get("enabled", 0),
            "paused": counts.get("paused", 0),
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "cancelling": counts.get("cancelling", 0),
            "resuming": counts.get("resuming", 0),
            "failed": counts.get("failed", 0),
            "interrupted": counts.get("interrupted", 0),
            "finished_with_errors": counts.get("finished_with_errors", 0),
            "finished": counts.get("finished", 0),
            "cancelled": counts.get("cancelled", 0),
        }

    async def events_for_task(
        self,
        *,
        task_id: uuid.UUID,
        after_seq: int | None = None,
        before_seq: int | None = None,
        event_type: list[str] | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
        limit: int = 500,
        descending: bool = False,
    ) -> list[TaskEvent]:
        stmt = select(TaskEvent).where(TaskEvent.task_id == task_id)
        if after_seq is not None:
            stmt = stmt.where(TaskEvent.id > after_seq)
        if before_seq is not None:
            stmt = stmt.where(TaskEvent.id < before_seq)
        if event_type:
            stmt = stmt.where(TaskEvent.event_type.in_(event_type))
        if from_ is not None:
            stmt = stmt.where(TaskEvent.ts >= from_)
        if to is not None:
            stmt = stmt.where(TaskEvent.ts <= to)
        order = TaskEvent.id.desc() if descending else TaskEvent.id.asc()
        result = await self.session.execute(stmt.order_by(order).limit(limit))
        rows = list(result.scalars().all())
        for row in rows:
            row.payload = await self._decrypt_document(
                tenant_id=row.tenant_id,
                resource_type="task",
                resource_id=str(row.task_id),
                purpose="task_event",
                record_id=str(row.encryption_record_id),
                key_id=row.payload_key_id,
                ciphertext=row.payload_ciphertext,
                nonce=row.payload_nonce,
            )
        return rows

    async def latest_event_seq(self, task_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.max(TaskEvent.id)).where(TaskEvent.task_id == task_id)
        )
        return int(result.scalar_one_or_none() or 0)

    async def event_counts_for_task(
        self,
        *,
        task_id: uuid.UUID,
        from_: datetime,
        to: datetime,
    ) -> dict[str, int]:
        """Return exact event-type counts for one authorized diagnostic window."""
        result = await self.session.execute(
            select(TaskEvent.event_type, func.count())
            .where(
                TaskEvent.task_id == task_id,
                TaskEvent.ts >= from_,
                TaskEvent.ts <= to,
            )
            .group_by(TaskEvent.event_type)
        )
        return {
            str(event_type): int(count)
            for event_type, count in result.all()
        }

    async def scheduled_execution_summary(
        self,
        *,
        schedule_id: uuid.UUID,
        from_: datetime,
        to: datetime,
    ) -> dict[str, Any]:
        """Aggregate scheduled execution status and duration without reading inputs."""
        result = await self.session.execute(
            select(
                ScheduledRunExecution.status,
                func.count(),
                func.count(
                    ScheduledRunExecution.finished_at
                    - ScheduledRunExecution.started_at
                ),
                func.avg(
                    func.extract(
                        "epoch",
                        ScheduledRunExecution.finished_at
                        - ScheduledRunExecution.started_at,
                    )
                    * 1000.0
                ),
            )
            .where(
                ScheduledRunExecution.schedule_id == schedule_id,
                ScheduledRunExecution.triggered_at >= from_,
                ScheduledRunExecution.triggered_at <= to,
            )
            .group_by(ScheduledRunExecution.status)
        )
        status_counts: dict[str, int] = {}
        duration_weighted_total = 0.0
        duration_count = 0
        for status, count, completed_count, average_duration_ms in result.all():
            normalized_count = int(count)
            status_counts[str(status)] = normalized_count
            if average_duration_ms is not None:
                duration_weighted_total += (
                    float(average_duration_ms) * int(completed_count)
                )
                duration_count += int(completed_count)
        total = sum(status_counts.values())
        failed = status_counts.get("failed", 0)
        return {
            "total": total,
            "status_counts": status_counts,
            "failures": failed,
            "failure_rate": (failed / total if total else 0.0),
            "average_duration_ms": (
                duration_weighted_total / duration_count
                if duration_count
                else None
            ),
        }

    async def create_schedule(
        self,
        *,
        task_id: uuid.UUID,
        schedule_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        workflow_id: str,
        name: str,
        enabled: bool,
        schedule_type: str,
        cron_expr: str | None,
        interval_seconds: int | None,
        timezone: str,
        input_preset: dict,
        mount_enabled: bool,
        notification_policy: dict,
        next_run_at: datetime | None,
        end_at: datetime | None = None,
        service_account_id: uuid.UUID | None = None,
    ) -> tuple[Task, TaskSchedule]:
        task_private = {
            "payload": {
                "name": name,
                "schedule_id": str(schedule_id),
                "schedule_type": schedule_type,
                "cron_expr": cron_expr,
                "interval_seconds": interval_seconds,
                "timezone": timezone,
                "next_run_at": next_run_at.isoformat() if next_run_at else None,
                "end_at": end_at.isoformat() if end_at else None,
                "last_status": None,
                "notification_policy": notification_policy,
            },
            "result": None,
            "error": None,
        }
        task_encrypted = await self._encrypt_document(
            tenant_id=tenant_id,
            resource_type="task",
            resource_id=str(task_id),
            purpose="task_private",
            record_id=str(task_id),
            value=task_private,
        )
        schedule_private = {
            "name": name,
            "input_preset": input_preset,
            "notification_policy": notification_policy,
        }
        schedule_encrypted = await self._encrypt_document(
            tenant_id=tenant_id,
            resource_type="task",
            resource_id=str(task_id),
            purpose="task_schedule_private",
            record_id=str(schedule_id),
            value=schedule_private,
        )
        task = Task(
            id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            owner_id=user_id,
            workflow_id=workflow_id,
            task_type="scheduled_run",
            status="enabled" if enabled else "paused",
            progress=0,
            content_ciphertext=task_encrypted.ciphertext,
            content_nonce=task_encrypted.nonce,
            content_key_id=task_encrypted.key_id,
            service_account_id=service_account_id,
        )
        schedule = TaskSchedule(
            id=schedule_id,
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
            workflow_id=workflow_id,
            enabled=enabled,
            schedule_type=schedule_type,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            timezone=timezone,
            private_ciphertext=schedule_encrypted.ciphertext,
            private_nonce=schedule_encrypted.nonce,
            private_key_id=schedule_encrypted.key_id,
            private_schema_version=2,
            mount_enabled=mount_enabled,
            next_run_at=next_run_at,
            end_at=end_at,
            service_account_id=service_account_id,
        )
        self.session.add(task)
        self.session.add(schedule)
        await self.session.flush()
        task.payload = task_private["payload"]
        task.result = None
        task.error = None
        schedule.input_preset = input_preset
        schedule.notification_policy = notification_policy
        schedule.name = name
        return task, schedule

    async def get_schedule_by_task(self, task_id: uuid.UUID) -> TaskSchedule | None:
        result = await self.session.execute(
            select(TaskSchedule).where(TaskSchedule.task_id == task_id)
        )
        schedule = result.scalar_one_or_none()
        return (
            await self._materialize_schedule(schedule)
            if schedule is not None
            else None
        )

    async def get_schedule(self, schedule_id: uuid.UUID) -> TaskSchedule | None:
        schedule = await self.session.get(TaskSchedule, schedule_id)
        return (
            await self._materialize_schedule(schedule)
            if schedule is not None
            else None
        )

    async def update_schedule(self, schedule_id: uuid.UUID, **fields: Any) -> TaskSchedule:
        allowed = {
            "name", "enabled", "schedule_type", "cron_expr", "interval_seconds",
            "timezone", "input_preset", "mount_enabled", "notification_policy",
            "next_run_at", "end_at", "last_run_at", "last_status",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"TasksRepo.update_schedule: unknown columns {sorted(unknown)}")
        schedule = await self.session.get(TaskSchedule, schedule_id)
        if schedule is None:
            raise LookupError(f"Schedule {schedule_id} not found")
        private_updates = _SCHEDULE_PRIVATE_FIELDS.intersection(fields)
        if private_updates:
            await self._materialize_schedule(schedule)
            private = {
                "name": schedule.name,
                "input_preset": schedule.input_preset,
                "notification_policy": schedule.notification_policy,
            }
            private.update({key: fields[key] for key in private_updates})
            await self._store_schedule_private(schedule, private)
        for key, value in fields.items():
            if key in _SCHEDULE_PRIVATE_FIELDS:
                continue
            setattr(schedule, key, value)
        await self.session.flush()
        return schedule

    async def create_scheduled_execution(
        self,
        *,
        execution_id: uuid.UUID,
        tenant_id: uuid.UUID,
        schedule_id: uuid.UUID,
        workflow_id: str,
        run_key: str,
        trigger_type: str,
        input_snapshot: dict,
        status: str = "queued",
    ) -> ScheduledRunExecution | None:
        existing = await self.session.execute(
            select(ScheduledRunExecution).where(
                ScheduledRunExecution.schedule_id == schedule_id,
                ScheduledRunExecution.run_key == run_key,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return None
        private = {
            "input_snapshot": input_snapshot,
            "result": None,
            "error": None,
            "run_state": {},
            "notification_state": {},
        }
        encrypted = await self._encrypt_document(
            tenant_id=tenant_id,
            resource_type="task_execution",
            resource_id=str(execution_id),
            purpose="scheduled_execution_private",
            record_id=str(execution_id),
            value=private,
        )
        execution = ScheduledRunExecution(
            id=execution_id,
            tenant_id=tenant_id,
            schedule_id=schedule_id,
            workflow_id=workflow_id,
            run_key=run_key,
            status=status,
            trigger_type=trigger_type,
            private_ciphertext=encrypted.ciphertext,
            private_nonce=encrypted.nonce,
            private_key_id=encrypted.key_id,
        )
        self.session.add(execution)
        await self.session.flush()
        execution.input_snapshot = input_snapshot
        execution.result = None
        execution.error = None
        execution.run_state = {}
        execution.notification_state = {}
        return execution

    async def get_scheduled_execution(
        self,
        execution_id: uuid.UUID,
    ) -> ScheduledRunExecution | None:
        execution = await self.session.get(ScheduledRunExecution, execution_id)
        return (
            await self._materialize_execution(execution)
            if execution is not None
            else None
        )

    async def list_scheduled_executions(
        self,
        *,
        schedule_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ScheduledRunExecution], int]:
        stmt = select(ScheduledRunExecution).where(
            ScheduledRunExecution.schedule_id == schedule_id
        )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        rows = await self.session.execute(
            stmt.order_by(desc(ScheduledRunExecution.triggered_at)).limit(limit).offset(offset)
        )
        count = await self.session.execute(count_stmt)
        executions = list(rows.scalars().all())
        for execution in executions:
            await self._materialize_execution(execution)
        return executions, int(count.scalar_one())

    async def update_scheduled_execution(
        self,
        execution_id: uuid.UUID,
        **fields: Any,
    ) -> None:
        allowed = {
            "status", "started_at", "finished_at", "result", "results_uri",
            "error", "run_state", "notification_state",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(
                f"TasksRepo.update_scheduled_execution: unknown columns {sorted(unknown)}"
            )
        execution = await self.session.get(ScheduledRunExecution, execution_id)
        if execution is None:
            raise LookupError(f"Scheduled execution {execution_id} not found")
        private_updates = _EXECUTION_PRIVATE_FIELDS.intersection(fields)
        if private_updates:
            await self._materialize_execution(execution)
            private = {
                "input_snapshot": execution.input_snapshot,
                "result": execution.result,
                "error": execution.error,
                "run_state": execution.run_state,
                "notification_state": execution.notification_state,
            }
            private.update({key: fields[key] for key in private_updates})
            await self._store_execution_private(execution, private)
        for key, value in fields.items():
            if key in _EXECUTION_PRIVATE_FIELDS:
                continue
            setattr(execution, key, value)
        await self.session.flush()
