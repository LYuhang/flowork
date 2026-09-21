"""Runtime-neutral interface for Flowork's durable background work.

The product and route layers depend on this module, not on DBOS directly. DBOS
is the current PostgreSQL-backed implementation and can be replaced without
changing task submission or cancellation call sites.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

from vibecanvas_api.config import config


BACKGROUND_APPLICATION_NAME = "flowork-background"


@dataclass(frozen=True, slots=True)
class BackgroundQueueSpec:
    name: str
    worker_concurrency: int
    polling_interval_sec: float = 0.5


def _queue_concurrency(default: int = 1) -> int:
    return max(1, min(int(config.background_queue_concurrency or default), 32))


QUEUE_SPECS: dict[str, BackgroundQueueSpec] = {
    name: BackgroundQueueSpec(name=name, worker_concurrency=_queue_concurrency())
    for name in (
        "interactive", "deployments", "kb_indexing", "control", "maintenance",
    )
}

# DBOS persists serialized arguments. Keep its durable history free of private
# business content by making every public task contract an opaque-record lookup.
BACKGROUND_JOB_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "batch_exec": frozenset({"task_id"}),
    "deployment_invoke": frozenset({"invocation_id"}),
    "kb.index_file": frozenset({"file_id"}),
    "scheduled_runs.execute": frozenset({"execution_id"}),
    "authorization.apply_mutation": frozenset({"mutation_id"}),
}


_client = None
_client_lock = threading.RLock()
_registered_queues: set[str] = set()


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            from dbos import DBOSClient

            _client = DBOSClient(
                system_database_url=config.dbos_system_database_url,
                application_name=BACKGROUND_APPLICATION_NAME,
                system_database_pool_size=config.dbos_client_pool_size,
                use_listen_notify=True,
            )
    return _client


def _ensure_queue(queue_name: str) -> None:
    if queue_name in _registered_queues:
        return
    try:
        spec = QUEUE_SPECS[queue_name]
    except KeyError as exc:
        raise ValueError(f"unknown background queue: {queue_name}") from exc
    with _client_lock:
        if queue_name in _registered_queues:
            return
        _get_client().register_queue(
            spec.name,
            worker_concurrency=spec.worker_concurrency,
            polling_interval_sec=spec.polling_interval_sec,
            on_conflict="never_update",
        )
        _registered_queues.add(queue_name)


def _validate_payload(workflow_name: str, kwargs: dict[str, Any]) -> None:
    expected = BACKGROUND_JOB_PAYLOAD_KEYS.get(workflow_name)
    if expected is None:
        raise ValueError(f"unknown background workflow: {workflow_name}")
    if set(kwargs) != expected:
        raise ValueError(
            f"background workflow {workflow_name} requires only opaque keys "
            f"{sorted(expected)}"
        )
    if any(not isinstance(kwargs[key], str) or not kwargs[key] for key in expected):
        raise ValueError("background workflow identifiers must be non-empty strings")


def enqueue_background_job(
    workflow_name: str,
    *,
    job_id: str,
    queue: str,
    kwargs: dict[str, Any],
    priority: int | None = None,
) -> str:
    """Durably enqueue one named background workflow.

    ``job_id`` is both the platform delivery identifier and the DBOS workflow
    id. Repeating the call is therefore idempotent and is used by the queued-row
    reconciler after an API/database commit succeeds but delivery is uncertain.
    """
    _validate_payload(workflow_name, kwargs)
    _ensure_queue(queue)
    options = _enqueue_options(
        workflow_name=workflow_name,
        job_id=job_id,
        queue=queue,
        priority=priority,
    )
    handle = _get_client().enqueue(options, dict(kwargs))
    return str(handle.get_workflow_id())


def _enqueue_options(
    *,
    workflow_name: str,
    job_id: str,
    queue: str,
    priority: int | None,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "workflow_name": workflow_name,
        "queue_name": queue,
        "workflow_id": str(job_id),
        "application_name": BACKGROUND_APPLICATION_NAME,
    }
    if priority is not None:
        options["priority"] = int(priority)
    return options


async def enqueue_background_job_async(
    workflow_name: str,
    *,
    job_id: str,
    queue: str,
    kwargs: dict[str, Any],
    priority: int | None = None,
) -> str:
    return await asyncio.to_thread(
        enqueue_background_job,
        workflow_name,
        job_id=job_id,
        queue=queue,
        kwargs=kwargs,
        priority=priority,
    )


async def enqueue_background_job_in_transaction(
    session,
    workflow_name: str,
    *,
    job_id: str,
    queue: str,
    kwargs: dict[str, Any],
    priority: int | None = None,
) -> str:
    """Enqueue atomically with a caller-owned async SQLAlchemy transaction.

    Flowork and DBOS share one PostgreSQL database. This closes both the
    worker-before-commit race and the commit-before-enqueue delivery gap for
    producers that do not have an external authorization side effect.
    """
    _validate_payload(workflow_name, kwargs)
    await asyncio.to_thread(_ensure_queue, queue)
    options = _enqueue_options(
        workflow_name=workflow_name,
        job_id=job_id,
        queue=queue,
        priority=priority,
    )
    connection = await session.connection()

    def _enqueue(sync_connection) -> str:
        handle = _get_client().enqueue_in_transaction(
            sync_connection,
            options,
            dict(kwargs),
        )
        return str(handle.get_workflow_id())

    return await connection.run_sync(_enqueue)


def cancel_background_job(job_id: str) -> None:
    _get_client().cancel_workflow(str(job_id), cancel_children=True)


async def cancel_background_job_async(job_id: str) -> None:
    await asyncio.to_thread(cancel_background_job, job_id)


def close_background_queue_client() -> None:
    global _client
    with _client_lock:
        client, _client = _client, None
        _registered_queues.clear()
    if client is not None:
        client.destroy()


__all__ = [
    "BACKGROUND_APPLICATION_NAME",
    "BACKGROUND_JOB_PAYLOAD_KEYS",
    "QUEUE_SPECS",
    "BackgroundQueueSpec",
    "cancel_background_job",
    "cancel_background_job_async",
    "close_background_queue_client",
    "enqueue_background_job",
    "enqueue_background_job_async",
    "enqueue_background_job_in_transaction",
]
