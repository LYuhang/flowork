"""Resubmit stuck queued tasks through the admin engine.

The atomic-submit route (POST /workflows/{wf_id}/batch) inserts the
``tasks`` row inside the request transaction, then does a best-effort DBOS
enqueue after commit. If delivery fails, the row stays ``queued``
forever and no worker picks it up.

A low-frequency DBOS safety schedule sweeps every five minutes for
``status='queued' AND submitted_at < now() - 60s`` and re-publishes the
task. DBOS uses ``background_job_id`` as its workflow id, so:

* a duplicate of a still-pending workflow is an idempotent no-op;
* a lost delivery triggers a fresh pick-up.

Uses the admin engine (RLS-bypassing) because this is a system-owned
cross-tenant sweep — there is no request context, no user, no tenant.

Only ``batch_exec`` rows are resubmitted here. Other background systems own
their own lifecycle and do not use the Task Center table.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from vibecanvas_api.services.background_queue import enqueue_background_job
from vibecanvas_api.services.queue_routing import route_for
from vibecanvas_api.storage.models_tasks import Task
from vibecanvas_api.storage.sync_session import short_admin_session


RECONCILER_INTERVAL_SEC = 300
STUCK_THRESHOLD_SEC = 60


_TASK_TYPE_TO_WORKFLOW_NAME: dict[str, str] = {
    "batch_exec": "batch_exec",
}


def _build_kwargs_for(task_type: str, row, payload: dict) -> dict:
    """Build the background workflow arguments for a stuck Task row.

    Each task's worker signature is its own — they share NO kwargs
    contract, so the dispatch is explicit per branch. Keep this in
    lockstep with :data:`_TASK_TYPE_TO_WORKFLOW_NAME`.

    Raises ``ValueError`` for an unknown ``task_type`` — the caller
    (:func:`_resubmit`) gates this via the dispatch table so it can
    quietly skip unmapped types; the raise here protects future callers
    that bypass the dispatch table.
    """
    if task_type == "batch_exec":
        return {"task_id": str(row.id)}
    raise ValueError(
        f"reconciler: no kwargs builder for task_type={task_type!r}"
    )


def resubmit_stuck_queued():
    """Background entry point — run the async sweep on a fresh event loop."""
    asyncio.run(_resubmit())


async def _resubmit() -> None:
    """Sweep ``tasks WHERE status='queued' AND stuck`` → re-publish.

    Read-only on the DB; the worker picking the re-published task is
    what flips ``status`` to ``running`` (no UPDATE here).

    The SELECT clause includes ``task_type`` (added in KB/RAG T6); the
    dispatch table + ``_build_kwargs_for`` together turn each row into
    the correct durable enqueue call.
    Unmapped types are skipped silently — see the module docstring TODO.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STUCK_THRESHOLD_SEC)
    async with short_admin_session() as session:
        rows = list((await session.execute(
            select(Task).where(
                Task.status == "queued",
                Task.submitted_at < cutoff,
            )
        )).scalars().all())
    for row in rows:
        name = _TASK_TYPE_TO_WORKFLOW_NAME.get(row.task_type)
        if name is None:
            # Unmapped task_type — sibling bug, out of scope; skip.
            continue
        kwargs = _build_kwargs_for(row.task_type, row, {})
        enqueue_background_job(
            name,
            job_id=row.background_job_id,
            queue=route_for(row.task_type),
            kwargs=kwargs,
        )
