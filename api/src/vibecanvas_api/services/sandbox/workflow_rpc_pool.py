"""Bounded logical executions across resident async workers of one revision.

Reservations include human waiting, persistence and confirmed cancellation.
A dead worker is replaced only for new calls; lost calls are never replayed.
Files and the resource budget remain shared for the sandbox's lifetime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from pathlib import Path
from itertools import count
from tempfile import TemporaryDirectory

from .workflow_rpc_slot import WorkflowRpcWorker, WorkflowInvocationSlot
from vibecanvas_api.services.deployment_completion import complete_before_cancelling


class WorkflowPoolFull(RuntimeError):
    pass


class WorkflowRpcPool:
    def __init__(self, *, capacity: int, factory: Callable[[int], WorkflowRpcWorker], worker_count: int = 1):
        if type(capacity) is not int or (capacity != -1 and capacity < 1):
            raise ValueError("execution capacity must be -1 or a positive integer")
        if type(worker_count) is not int or worker_count < 1 or (capacity != -1 and capacity % worker_count):
            raise ValueError("capacity must be a positive multiple of worker_count")
        self.worker_count = worker_count
        self.worker_concurrency = -1 if capacity == -1 else capacity // worker_count
        self._next_worker = 0
        self.capacity = capacity
        self.factory = factory
        self._slots: dict[int, WorkflowInvocationSlot] = {}
        self._workers: dict[int, WorkflowRpcWorker] = {}
        self._owners: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._workspace = None
        self.workflow = None
        self.node_id = None

    @classmethod
    def for_session(cls, *, session, revision: str, workflow: dict, capacity: int, node_id: str | None = None, worker_count: int = 1, artifacts_root: str | None = None):
        """Project only user-visible roots; never the Chat credential volume."""
        workspace = TemporaryDirectory(prefix="fw-rpc-")
        workflow = deepcopy(workflow)
        destinations = {f"/{name}" for name in session.workspace_folders} | {"/mount"}
        binds = [(dest, src) for dest, src in session._rw_binds if dest in destinations]
        readonly = [("/skills", session.skills_dir)] if session.skills_dir else []
        pool = cls(
            capacity=capacity,
            worker_count=worker_count,
            factory=lambda index: WorkflowRpcWorker(
                provider=session.provider,
                root=str(Path(workspace.name) / str(index)),
                artifacts_root=artifacts_root or str(Path(workspace.name) / "artifacts"),
                revision=revision,
                workflow=workflow,
                node_id=node_id,
                rw_binds=binds,
                ro_binds=readonly,
            ),
        )
        pool._workspace = workspace
        pool.workflow = workflow
        pool.node_id = node_id
        return pool

    @property
    def busy(self) -> bool:
        return bool(self._owners)

    @property
    def accepting(self) -> bool:
        """The pool can acquire slots, replacing dead workers on demand.

        Losing the last warm worker does not retire its deployment session.
        Capacity admission is still enforced separately by acquire().
        """
        return not self._closed

    @property
    def ready(self) -> bool:
        return not self._closed and any(slot.alive for slot in self._slots.values())

    @asynccontextmanager
    async def acquire(self, execution_id: str):
        async with self._lock:
            if self._closed:
                raise RuntimeError("execution pool is retired")
            if execution_id in self._owners.values():
                raise RuntimeError("execution already has an owner")
            counts = [sum(index % self.worker_count == worker for index in self._owners)
                      for worker in range(self.worker_count)]
            available = [worker for worker, count in enumerate(counts) if self.worker_concurrency == -1 or count < self.worker_concurrency]
            if not available:
                raise WorkflowPoolFull("concurrency_limit_exceeded")
            worker_index = min(available, key=lambda worker: (
                counts[worker], (worker - self._next_worker) % self.worker_count))
            self._next_worker = (worker_index + 1) % self.worker_count
            indices = count(worker_index, self.worker_count) if self.capacity == -1 else range(worker_index, self.capacity, self.worker_count)
            index = next(i for i in indices if i not in self._owners)
            worker = self._workers.get(worker_index)
            if worker is None:
                worker = self.factory(worker_index)
                worker.capacity = self.worker_concurrency
                self._workers[worker_index] = worker
            slot = self._slots.get(index)
            if slot is None or not slot.alive:
                slot = WorkflowInvocationSlot(worker)
                self._slots[index] = slot
            self._owners[index] = execution_id
        try:
            await slot.start()
            if self._closed:
                await slot.close()
                raise RuntimeError("execution pool is retired")
            yield slot
        finally:
            # Release logical capacity only after its execution really stops.
            # A failed cancellation keeps this reservation; it never kills a
            # shared process or interrupts sibling calls.
            try:
                if slot is not None and slot.invocation_id is not None:
                    await slot.close()
            finally:
                # A failed stop keeps the reservation: capacity must never be
                # recycled while side effects might still be running.
                if slot is None or slot.invocation_id is None or not slot.alive:
                    async with self._lock:
                        self._owners.pop(index, None)
                        if self._closed and not self._owners and all(w.handle is None for w in self._workers.values()):
                            self._cleanup_workspace()

    async def prewarm(self) -> None:
        # Hold each reservation until every configured worker is started.
        async with AsyncExitStack() as stack:
            for index in range(self.worker_count):
                await stack.enter_async_context(self.acquire(f"prewarm-{index}"))

    async def retire(self) -> bool:
        async with self._lock:
            if self._owners:
                return False
            self._closed = True
        await self.close()
        return True

    def _cleanup_workspace(self):
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None

    @complete_before_cancelling
    async def close(self) -> None:
        """Force process loss on session shutdown; never resume those runs."""
        async with self._lock:
            self._closed = True
        outcomes = await asyncio.gather(*(worker.close() for worker in self._workers.values()), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        if not self._owners:
            self._slots.clear()
            self._cleanup_workspace()
