"""Bounded resident execution slots; acquisition never queues a deployment.

Reservations cover startup, human waiting, terminal persistence and cleanup.
Only the owning caller may release its slot. A dead slot can be replaced for a
new invocation, never to replay the invocation that died with its process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from .workflow_rpc_slot import WorkflowRpcSlot


class WorkflowPoolFull(RuntimeError):
    pass


class WorkflowRpcPool:
    def __init__(self, *, capacity: int, factory: Callable[[int], WorkflowRpcSlot]):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("execution capacity must be a positive integer")
        self.capacity = capacity
        self.factory = factory
        self._slots: dict[int, WorkflowRpcSlot] = {}
        self._owners: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._workspace = None
        self.workflow = None
        self.node_id = None

    @classmethod
    def for_session(cls, *, session, revision: str, workflow: dict, capacity: int, node_id: str | None = None):
        """Project only user-visible roots; never the Chat credential volume."""
        workspace = TemporaryDirectory(prefix="fw-rpc-")
        workflow = deepcopy(workflow)
        destinations = {f"/{name}" for name in session.workspace_folders} | {"/mount"}
        binds = [(dest, src) for dest, src in session._rw_binds if dest in destinations]
        readonly = [("/skills", session.skills_dir)] if session.skills_dir else []
        pool = cls(
            capacity=capacity,
            factory=lambda index: WorkflowRpcSlot(
                provider=session.provider,
                root=str(Path(workspace.name) / str(index)),
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
    def ready(self) -> bool:
        return not self._closed and any(slot.alive for slot in self._slots.values())

    @asynccontextmanager
    async def acquire(self, execution_id: str):
        async with self._lock:
            if self._closed:
                raise RuntimeError("execution pool is retired")
            if execution_id in self._owners.values():
                raise RuntimeError("execution already has an owner")
            index = next((i for i in range(self.capacity) if i not in self._owners), None)
            if index is None:
                raise WorkflowPoolFull("concurrency_limit_exceeded")
            self._owners[index] = execution_id
            slot = self._slots.get(index)
        try:
            if slot is not None and not slot.alive:
                await slot.close()
                slot = None
            if slot is None:
                slot = self.factory(index)
                self._slots[index] = slot
            await slot.start()
            if self._closed:
                await slot.close()
                raise RuntimeError("execution pool is retired")
            yield slot
        finally:
            # An interrupted driver must not leave a live process available to
            # another caller. Normal completion clears invocation_id only after
            # terminal ACK and artifact persistence.
            try:
                if slot is not None and slot.invocation_id is not None:
                    await slot.close()
            finally:
                # A failed kill keeps the reservation: capacity must never be
                # recycled while side effects might still be running.
                if slot is None or slot.invocation_id is None or not slot.alive:
                    async with self._lock:
                        self._owners.pop(index, None)
                        if self._closed and not self._owners:
                            self._cleanup_workspace()

    async def prewarm(self) -> None:
        async with self.acquire("prewarm"):
            pass

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

    async def close(self) -> None:
        """Force process loss on session shutdown; never resume those runs."""
        async with self._lock:
            self._closed = True
        for slot in self._slots.values():
            await slot.close()
        if not self._owners:
            self._slots.clear()
            self._cleanup_workspace()
