"""Per-execution approval futures, deadlines and idempotent decisions."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

EventSink = Callable[[dict], Awaitable[None]]


class ApprovalConflict(ValueError):
    """An expired, cancelled or already differently resolved approval."""


@dataclass
class Approval:
    approval_id: str
    node_id: str
    deadline: float
    monotonic_deadline: float
    future: asyncio.Future
    decision: bool | None = None
    reason: str | None = None
    decided_at: float | None = None
    resolved: asyncio.Event = field(default_factory=asyncio.Event)
    resumed: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "node_id": self.node_id,
            "deadline": self.deadline,
            "approved": self.decision,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }


class ApprovalBroker:
    """Owned by a single asyncio loop, so resolution has no check/update race.

    Only approved/rejected/timeout are graph decisions. Runtime cancellation
    interrupts the coroutine without manufacturing an approved=false output.
    The host must authenticate the actor BEFORE calling decide().
    """

    def __init__(self, emit: EventSink, stop: asyncio.Event, *, require_resume: bool = False):
        self._emit = emit
        self._stop = stop
        self.require_resume = require_resume
        self.approvals: dict[str, Approval] = {}

    @property
    def waiting(self) -> bool:
        return any(
            item.reason is None or (self.require_resume and not item.resumed.is_set())
            for item in self.approvals.values()
        )

    def _resolve(self, item: Approval, approved: bool, reason: str) -> None:
        if item.reason is not None:
            return
        item.decision = approved
        item.reason = reason
        item.decided_at = time.time()
        item.future.set_result(approved)

    async def decide(self, approval_id: str, approved: bool) -> dict:
        if type(approved) is not bool:
            raise ValueError("approved must be a boolean")
        item = self.approvals.get(approval_id)
        if item is None:
            raise KeyError(approval_id)
        if self._stop.is_set() or item.reason == "cancelled":
            raise ApprovalConflict("execution is no longer awaiting approval")
        if item.reason is None and asyncio.get_running_loop().time() >= item.monotonic_deadline:
            self._resolve(item, False, "timeout")
        reason = "approved" if approved else "rejected"
        if item.reason is not None and item.reason != reason:
            raise ApprovalConflict("approval already resolved")
        self._resolve(item, approved, reason)
        await item.resolved.wait()
        return item.public()

    def resume(self, approval_id: str, refresh: Callable[[], None]) -> dict:
        item = self.approvals.get(approval_id)
        if item is None:
            raise KeyError(approval_id)
        if self._stop.is_set() or item.reason == "cancelled":
            raise ApprovalConflict("execution is no longer awaiting approval")
        if not self.require_resume or item.reason is None or not item.resolved.is_set():
            raise ApprovalConflict("approval is not ready to resume")
        if not item.resumed.is_set():
            # No await between credential replacement and opening the gate.
            # A retried RPC must not overwrite credentials after continuation.
            refresh()
            item.resumed.set()
        return item.public()

    async def __call__(self, node, inputs: dict) -> bool:
        loop = asyncio.get_running_loop()
        seconds = node.node_config["timeout_seconds"]
        item = Approval(
            uuid.uuid4().hex, node.node_id, time.time() + seconds, loop.time() + seconds, loop.create_future()
        )
        self.approvals[item.approval_id] = item
        cancel_wait = asyncio.create_task(self._stop.wait())
        try:
            await self._emit(
                {
                    "type": "approval_requested",
                    **item.public(),
                    "instruction": node.node_config["instruction"],
                    "approver_email": node.node_config.get("approver_email", ""),
                    "inputs": inputs,
                }
            )
            remaining = max(0, item.monotonic_deadline - loop.time())
            await asyncio.wait({item.future, cancel_wait}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if self._stop.is_set():
                raise asyncio.CancelledError
            if item.reason is None:
                self._resolve(item, False, "timeout")
            if self.require_resume:
                await self._emit({"type": "approval_ready", **item.public()})
                item.resolved.set()  # decision RPC completes before host reauthorization
                resume_wait = asyncio.create_task(item.resumed.wait())
                try:
                    await asyncio.wait({resume_wait, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
                    if self._stop.is_set():
                        raise asyncio.CancelledError
                finally:
                    resume_wait.cancel()
                    await asyncio.gather(resume_wait, return_exceptions=True)
            await self._emit({"type": "approval_resolved", **item.public()})
            return item.decision
        finally:
            if item.reason is None:
                item.reason = "cancelled"
                item.decided_at = time.time()
                item.future.cancel()
            item.resolved.set()
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)
