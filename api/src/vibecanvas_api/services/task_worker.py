"""Single-start, fenced Task ownership; never replay unknown side effects.

Heartbeats are control-plane liveness, not a workflow duration limit. Every
worker write locks and checks the owning business row in the same transaction.
Recovery can revoke a token without a late worker overwriting the outcome.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, update

from vibecanvas_api.storage.models_tasks import ScheduledRunExecution, Task
from vibecanvas_api.storage.sync_session import run_in_short_session

HEARTBEAT_SECONDS = 10
STALE_SECONDS = 120


class WorkerOwnershipLost(RuntimeError):
    """The current process is no longer allowed to publish this attempt."""


@dataclass(frozen=True)
class WorkerClaim:
    kind: str
    resource_id: UUID
    token: UUID

    @property
    def model(self):
        return Task if self.kind == "batch" else ScheduledRunExecution

    @property
    def scope_id(self):
        return f"{self.kind}-{self.resource_id}-{self.token.hex}"


current_claim: ContextVar[WorkerClaim | None] = ContextVar("task_worker_claim", default=None)


async def claim_worker(session, kind: str, resource_id: UUID, *, delivery_id: str | None = None):
    if kind not in {"batch", "schedule"}:
        raise ValueError("Unknown Task worker kind.")
    model = Task if kind == "batch" else ScheduledRunExecution
    row = await session.get(model, resource_id, with_for_update=True, populate_existing=True)
    if row is None:
        return None
    if kind == "batch" and (row.task_type != "batch_exec" or
            (delivery_id is not None and row.background_job_id != delivery_id)):
        return None
    if row.status not in ({"queued", "resuming"} if kind == "batch" else {"queued"}):
        return None
    row.worker_token = uuid4()
    row.worker_recovery_pending = False
    row.worker_heartbeat_at = func.now()
    row.started_at = datetime.now(timezone.utc)
    row.finished_at = None
    row.status = "running"
    await session.flush()
    return WorkerClaim(kind, resource_id, row.worker_token)


async def assert_worker_owner(session):
    claim = current_claim.get()
    if claim is None:
        return  # Non-worker callers own their normal authorization transaction.
    row = await session.get(claim.model, claim.resource_id, with_for_update=True, populate_existing=True)
    if row is None or row.worker_token != claim.token or row.worker_recovery_pending:
        raise WorkerOwnershipLost("Task worker ownership was revoked; results must not be republished.")


async def heartbeat_worker(session, claim: WorkerClaim) -> bool:
    updated = await session.execute(update(claim.model).where(
        claim.model.id == claim.resource_id,
        claim.model.worker_token == claim.token,
        claim.model.worker_recovery_pending.is_(False),
    ).values(worker_heartbeat_at=func.now()).returning(claim.model.id))
    return updated.scalar_one_or_none() is not None


async def watch_worker(claim: WorkerClaim, stop) -> None:
    """Fail closed on ownership or database loss; recovery reconciles later."""
    while not stop.is_set():
        try:
            alive = await asyncio.to_thread(run_in_short_session,
                lambda session: heartbeat_worker(session, claim))
        except Exception:
            stop.set()
            raise WorkerOwnershipLost("Cannot renew Task worker ownership.") from None
        if not alive:
            stop.set()
            raise WorkerOwnershipLost("Task worker ownership was revoked.")
        await asyncio.sleep(HEARTBEAT_SECONDS)
