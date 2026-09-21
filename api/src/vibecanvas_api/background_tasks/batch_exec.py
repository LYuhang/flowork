"""Background implementation for user-visible ``batch_exec`` tasks.

The task delegates row execution to ``services.batch_runtime`` so Task-page and
Workflow-page batch execution share one runtime contract: one task-scoped
sandbox, in-sandbox bounded row workers, row-level ledgers, and ordered final
artifacts.

Event ordering: ``INSERT`` the ``task_events`` row first, then best-effort
``redis.publish``. The DB row is the authoritative event log; SSE consumers
fall back to polling if Redis is down. Event types use the unified protocol:
``state | progress | log | result | terminal``.

Cancellation is durable: the Task row is authoritative and a watcher mirrors
its state into a task-local ``threading.Event``. The shared batch runtime stops
waiting for unfinished rows, writes them as ``cancelled``, uploads partial
artifacts, and leaves the task resumable.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import suppress
from datetime import datetime, timezone

import redis
from vibecanvas_api.config import config
from vibecanvas_api.services.batch_runtime import BatchProgress, run_batch_workflow
from vibecanvas_api.services.llm_credentials_inject import (
    inject_into_run_context_sync,
)
from vibecanvas_api.services.redis_channels import (
    task_event_channel,
    task_event_envelope,
)
from vibecanvas_api.services.sandbox.coordinator import (
    dispose_sandbox_rpc_client,
)
from vibecanvas_api.storage.db import dispose_engine
from vibecanvas_api.storage.repo_service_accounts import (
    ServiceAccountLease,
    ServiceAccountsRepo,
)
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.sync_repo import SyncWorkflowRepo
from vibecanvas_api.storage.sync_session import (
    current_sync_tenant_id,
    run_in_short_session,
)

# --- best-effort redis publish ---------------------------------------------
#
# In production this notifies SSE consumers immediately. In the sandbox
# (no Redis daemon) every call fails fast and is swallowed — the DB
# task_events rows are the source of truth, and SSE consumers fall back
# to polling. Short connect/socket timeouts keep retry latency bounded.

def _publish(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    message: dict,
) -> None:
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
        # Best-effort — DB rows in task_events remain authoritative.
        pass


# --- short-session helpers --------------------------------------------------
#
# Both ``_emit`` and ``_update`` go through ``run_in_short_session`` so
# each DB write opens its own NullPool engine + async session,
# transaction-bound to a fresh ``asyncio.run``. This avoids reusing a
# loop-bound engine: the background worker body makes many sequential
# writes per task, so reusing a single loop-bound pool would crash on
# call #2.
#
# ``current_sync_tenant_id`` is set ONCE at the top of the task body;
# both helpers inherit it because ``run_in_short_session`` reads the CV
# in the caller's context.

def _emit(
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    event_type: str,
    payload: dict,
) -> None:
    """§8.2 ordering: INSERT first, then best-effort publish."""
    async def _runner(session) -> int:
        repo = TasksRepo(session)
        return await repo.insert_event(task_id, event_type, payload, tenant_id)

    ev_id = run_in_short_session(_runner)
    _publish(
        task_id,
        tenant_id,
        {"id": ev_id, "event_type": event_type, "payload": payload},
    )


def _update(task_id: uuid.UUID, **fields: object) -> None:
    """Update whitelisted fields on the ``Task`` row in a short session."""
    if not fields:
        return

    async def _runner(session) -> None:
        repo = TasksRepo(session)
        await repo.update_status(task_id, **fields)

    run_in_short_session(_runner)


def _task_snapshot(task_id: uuid.UUID) -> dict:
    async def _runner(session) -> dict:
        t = await TasksRepo(session).get(task_id)
        if t is None:
            return {}
        return {
            "payload": t.payload or {},
            "result": t.result or {},
            "results_uri": t.results_uri,
            "status": t.status,
            "progress": float(t.progress or 0),
        }

    return run_in_short_session(_runner)


async def _watch_durable_cancel(
    task_id: uuid.UUID,
    stop_event: threading.Event,
    *,
    poll_seconds: float = 0.25,
) -> None:
    """Mirror the durable Task cancellation state into the worker event.

    DBOS cancellation removes queued delivery and marks its workflow cancelled,
    while the database remains the authoritative business-level soft-cancel
    channel for partial results. Polling runs in a thread because the sync
    short-session bridge owns its own event loop.
    """
    while not stop_event.is_set():
        snapshot = await asyncio.to_thread(_task_snapshot, task_id)
        if snapshot.get("status") in {"cancelling", "cancelled"}:
            stop_event.set()
            return
        await asyncio.sleep(max(0.05, poll_seconds))


def _task_execution_lease(
    task_id: uuid.UUID,
    *,
    workflow_id: str,
) -> ServiceAccountLease:
    """Resolve the authoritative actor at pickup; queue identity is untrusted."""
    async def _runner(session) -> ServiceAccountLease:
        task = await TasksRepo(session).get(task_id)
        if (
            task is None
            or task.workflow_id != workflow_id
            or task.service_account_id is None
        ):
            raise LookupError("service_account_unavailable")
        return await ServiceAccountsRepo(session).require_active_lease(
            service_account_id=task.service_account_id,
            owner_resource_type="task",
            owner_resource_id=str(task_id),
        )

    return run_in_short_session(_runner)


# --- the task --------------------------------------------------------------

def batch_exec(
    *,
    task_id: str,
    tenant_id: str,
    user_id: str,
    workflow_id: str,
    data_source: dict,
    column_mapping: dict,
    output: dict | None = None,
    output_columns: list | None = None,
    concurrency: int = 1,
    resume: bool = False,
):
    """Run ``workflow_id`` once per row of ``data_source.rows``.

    ``concurrency`` (clamped 1..16) rows run in parallel on a thread pool. Each
    row builds its OWN run workspace (own run_id), so parallel rows never share
    a ``/run`` — the concurrency value is the batch's resource ceiling. Threads
    (not processes) because the work is LLM/IO-bound and shares one warm process.

    Args:
        task_id: UUID string identifying the ``tasks`` row.
        tenant_id: UUID string used for RLS + the ``tenant_id`` column
            on the emitted ``task_events`` rows.
        user_id: Username for the ``SyncWorkflowRepo`` facade (the agent
            uses the same username plumbing).
        workflow_id: ``workflows.wf_id`` (string id, not UUID).
        data_source: ``{"rows": [ {col: val, ...}, ... ]}``.
        column_mapping: ``{csv_column: workflow_input_field}``. Columns
            not in the map pass through with their original name.

    Side effects:
        * Sets ``current_sync_tenant_id`` for the lifetime of the call
          so every sync repo invocation (workflow load + per-write
          short session) is RLS-scoped.
        * INSERTs into ``task_events`` using the unified task event protocol:
          ``state`` (batch started), ``progress`` (per row), and one
          ``terminal`` event.
        * UPDATEs the ``tasks`` row's ``status`` / ``progress`` /
          ``result`` / ``results_uri`` / ``started_at`` / ``finished_at``
          as the run proceeds.
        * Uploads ``tasks/{task_id}/results.csv`` to the configured
          object store on success.
    """
    stop_event = threading.Event()

    t_uuid = uuid.UUID(task_id)
    tn_uuid = uuid.UUID(tenant_id)

    # Every synchronous repository call inside this task body
    # (workflow load via SyncWorkflowRepo, every _emit / _update short
    # session) reads this CV. Set ONCE here at the top.
    current_sync_tenant_id.set(tenant_id)

    rows = data_source.get("rows", []) or []
    row_count = len(rows)
    workers = max(1, min(int(concurrency or 1), 16))

    async def _on_progress(p: BatchProgress) -> None:
        # Cancelled rows are bookkeeping for partial artifacts, not newly
        # completed business work. Keep the user-visible progress frozen at
        # the moment cancellation was requested.
        if p.status == "cancelled":
            return
        await asyncio.to_thread(_emit, t_uuid, tn_uuid, "progress", {
            "schema_version": 1,
            "level": "info",
            "category": "batch",
            "action": "batch.progress",
            "message": f"{p.done} / {p.total} rows completed.",
            "task_status": "resuming" if resume else "running",
            "sandbox_status": "running",
            "scope": {"type": "row", "id": str(p.index), "name": None},
            "progress": {
                "done": p.done,
                "total": p.total,
                "unit": "rows",
                "percent": p.done / max(p.total, 1),
            },
            "data": {
                "row_index": p.index,
                "row_status": p.status,
            },
            "error": (
                {
                    "code": "row_execution_error",
                    "message": str((p.row.get("error") or {}).get("message") or p.row.get("error")),
                    "retryable": False,
                    "details": {"row_index": p.index},
                }
                if p.row.get("error")
                else None
            ),
        })
        await asyncio.to_thread(_update, t_uuid, progress=p.done / max(p.total, 1))

    try:
        lease = _task_execution_lease(t_uuid, workflow_id=workflow_id)
        effective_user_id = str(lease.created_by)
        # Transition queued → running. ``started_at`` is the wall clock at
        # task start (the DB has no NOW() default for this column).
        _update(
            t_uuid,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        task_snapshot = _task_snapshot(t_uuid) if resume else {}
        prior_result = task_snapshot.get("result") or {}
        previous_results_uri = (
            (prior_result.get("artifact_uris") or {}).get("jsonl")
            if isinstance(prior_result, dict)
            else None
        )
        resume_count = int((prior_result or {}).get("resume_count") or 0) + (1 if resume else 0)
        _emit(t_uuid, tn_uuid, "state", {
            "schema_version": 1,
            "level": "info",
            "category": "batch",
            "action": "batch.resume_started" if resume else "batch.started",
            "message": (
                f"Batch execution resumed for {row_count} row(s)."
                if resume else
                f"Batch execution started for {row_count} row(s)."
            ),
            "task_status": "running",
            "sandbox_status": "running",
            "scope": {"type": "task", "id": task_id, "name": None},
            "progress": {
                "done": 0,
                "total": row_count,
                "unit": "rows",
                "percent": 0,
            },
            "data": {"row_count": row_count, "concurrency": workers},
            "error": None,
        })

        workflow_dict = SyncWorkflowRepo(username=effective_user_id).get_current_workflow(
            workflow_id
        )
        prepared_run_extra = inject_into_run_context_sync(
            {},
            workflow_dict,
            tenant_id=tenant_id,
            user_id=effective_user_id,
            workflow_id=workflow_id,
            execution_id=task_id,
            execution_resource_type="task",
            principal_type="service_account",
            principal_id=str(lease.service_account_id),
            principal_generation=lease.generation,
        )
        async def _run_on_isolated_worker_loop():
            # A prefork worker process handles many tasks, while each sync
            # background worker body opens a fresh asyncio.run loop. Detach any pooled
            # connections left by a different task loop, and close this loop's
            # pool before asyncio.run tears the loop down.
            await dispose_engine(close=False)
            cancel_watcher = asyncio.create_task(
                _watch_durable_cancel(t_uuid, stop_event)
            )
            try:
                return await run_batch_workflow(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=effective_user_id,
                    workflow_id=workflow_id,
                    workflow=workflow_dict,
                    rows=rows,
                    column_mapping=column_mapping or {},
                    output=output,
                    output_columns=output_columns,
                    concurrency=workers,
                    previous_results_uri=previous_results_uri,
                    resume_count=resume_count,
                    on_progress=_on_progress,
                    stop_event=stop_event,
                    execution_principal_type="service_account",
                    execution_principal_id=str(lease.service_account_id),
                    execution_principal_generation=lease.generation,
                    prepared_run_extra=prepared_run_extra,
                )
            finally:
                cancel_watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_watcher
                # The worker opens a new asyncio.run loop for every task.
                # Close grpc.aio while it is still on the loop that created it
                # so the next batch cannot inherit a dead-loop channel.
                await dispose_sandbox_rpc_client()
                await dispose_engine()

        batch_result = asyncio.run(_run_on_isolated_worker_loop())
        summary = batch_result.summary
        final_status = batch_result.status
        final_snapshot = _task_snapshot(t_uuid)
        visible_progress = float(final_snapshot.get("progress") or 0)
        terminal_fields = {
            "status": final_status,
            "result": summary,
            "results_uri": batch_result.results_uri,
            "finished_at": datetime.now(timezone.utc),
        }
        if final_status != "interrupted":
            terminal_fields["progress"] = 1.0
            visible_progress = 1.0
        _update(t_uuid, **terminal_fields)
        _emit(t_uuid, tn_uuid, "terminal", {
            "schema_version": 1,
            "level": "info" if final_status in {"finished", "interrupted"} else "warning",
            "category": "task",
            "action": "task.cancelled" if final_status == "interrupted" else f"task.{final_status}",
            "message": {
                "finished": "Task finished.",
                "finished_with_errors": "Task finished with row errors.",
                "interrupted": "Task cancelled. Partial results can be resumed.",
            }.get(final_status, f"Task ended with status {final_status}."),
            "task_status": final_status,
            "sandbox_status": "released",
            "scope": {"type": "task", "id": task_id, "name": None},
            "progress": {
                "done": round(visible_progress * row_count),
                "total": row_count,
                "unit": "rows",
                "percent": visible_progress,
            },
            "data": {"summary": summary, "results_uri": batch_result.results_uri},
            "error": None,
        })
    except Exception as exc:
        # Task-level failure (not per-row): record on the row + emit a
        # terminal error event so the SSE consumer / UI can close the
        # stream.
        cancel_snapshot = _task_snapshot(t_uuid)
        if stop_event.is_set() or cancel_snapshot.get("status") in {
            "cancelling",
            "cancelled",
        }:
            # Cleanup failures must not replace an accepted user
            # cancellation with a red business-error state.
            _update(
                t_uuid,
                status="cancelled",
                finished_at=datetime.now(timezone.utc),
            )
            _emit(t_uuid, tn_uuid, "terminal", {
                "schema_version": 1,
                "level": "info",
                "category": "task",
                "action": "task.cancelled",
                "message": "Task cancelled.",
                "task_status": "cancelled",
                "sandbox_status": "released",
                "scope": {"type": "task", "id": task_id, "name": None},
                "progress": None,
                "data": {},
                "error": None,
            })
            return
        service_account_error = (
            isinstance(exc, LookupError)
            and str(exc) == "service_account_unavailable"
        )
        error_code = (
            "service_account_unavailable"
            if service_account_error
            else "task_execution_error"
        )
        err_msg = (
            "The batch execution identity is unavailable."
            if service_account_error
            else f"{type(exc).__name__}: {exc}"
        )
        try:
            _update(
                t_uuid,
                status="failed",
                error=err_msg,
                finished_at=datetime.now(timezone.utc),
            )
            _emit(t_uuid, tn_uuid, "terminal", {
                "schema_version": 1,
                "level": "error",
                "category": "task",
                "action": "task.failed",
                "message": err_msg,
                "task_status": "failed",
                "sandbox_status": "released",
                "scope": {"type": "task", "id": task_id, "name": None},
                "progress": None,
                "data": {},
                "error": {
                    "code": error_code,
                    "message": err_msg,
                    "retryable": False,
                    "details": {},
                },
            })
        except Exception:
            # Last-resort: even the failure-write failed (DB down?).
            # Re-raise the ORIGINAL exception so DBOS records it and
            # the supervisor / reconciler (T11/T15) can clean up.
            pass
        raise
