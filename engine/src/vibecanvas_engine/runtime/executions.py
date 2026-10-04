"""In-memory ownership for RPC workflow calls, independent of HTTP connections.

The host persists events/results before acknowledging them. Acknowledgement
releases payload memory, never permits an invocation ID to execute twice. No
request inputs, workflow definitions, credentials or results are written here.
"""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid

from ..utils import normalize_inputs_for_fields, start_node_input_fields
from ..workflow import Workflow
from .approvals import ApprovalBroker
from .single_node import node_events, validate_selected_node


class ExecutionCapacityError(RuntimeError):
    pass


class ExecutionConflict(ValueError):
    pass


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _credential_tokens(context: dict) -> set[str]:
    tokens = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"api_key", "capability"} and isinstance(item, str) and item:
                    tokens.add(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for key in ("llm_credentials", "workflow_resources"):
        collect(context.get(key))
    return tokens


def _error_message(value, tokens: set[str]) -> str:
    if isinstance(value, dict):
        value = value.get("error_message", "execution_failed")
    message = value if isinstance(value, str) else "execution_failed"
    for token in sorted(tokens, key=len, reverse=True):
        message = message.replace(token, "[REDACTED]")
    return message


@dataclass
class Execution:
    invocation_id: str
    fingerprint: str
    revision: str
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    events: deque = field(default_factory=deque)
    seq: int = 0
    acknowledged: int = 0
    status: str = "running"
    cancel_requested: bool = False
    timed_out: bool = False
    approval_wait_started: float | None = None
    approval_wait_seconds: float = 0.0
    task: asyncio.Task | None = None
    approvals: ApprovalBroker | None = None
    result: dict | None = None
    buffered_bytes: int = 0
    context: dict = field(default_factory=dict, repr=False)
    private_tokens: set[str] = field(default_factory=set, repr=False)
    active_nodes: set[str] = field(default_factory=set)


class WorkflowRuntime:
    """All methods run on one event loop; callers provide their own transport.

    There is no execution restart or disk checkpoint. A new runtime has a new
    generation. The host fences old messages and marks lost executions failed.
    """

    TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled"})
    CONTEXT_KEYS = frozenset({"llm_credentials", "workflow_resources", "code_pythonpath", "run_dir", "workflow_resume_visits"})

    def __init__(self, *, capacity: int, max_pending_results: int = 32, max_buffer_bytes: int = 16 * 1024 * 1024):
        if type(capacity) is not int or (capacity != -1 and capacity < 1):
            raise ValueError("capacity must be -1 or positive")
        self.generation = uuid.uuid4().hex
        self.capacity = capacity
        self.max_pending_results = max_pending_results
        self.max_buffer_bytes = max_buffer_bytes
        self.workflows: dict[str, tuple[str, dict, str | None]] = {}
        self.executions: dict[str, Execution] = {}
        # Lightweight tombstones survive payload ACKs for this runtime's life.
        self.completed: dict[str, tuple[str, str]] = {}

    def install(self, revision: str, workflow: dict, node_id: str | None = None) -> None:
        fingerprint = _digest({"workflow": workflow, "node_id": node_id})
        previous = self.workflows.get(revision)
        if previous:
            if previous[0] != fingerprint:
                raise ExecutionConflict("revision already has another workflow")
            return
        if self.workflows:
            raise ExecutionConflict("a resident runtime owns exactly one revision")
        if node_id is None:
            validation = Workflow.check(workflow)
            if validation["status"] != "success":
                raise ValueError(validation["error_message"])
        else:
            validate_selected_node(workflow, node_id)
        self.workflows[revision] = (fingerprint, deepcopy(workflow), node_id)

    def invoke(
        self,
        invocation_id: str,
        revision: str,
        inputs: dict,
        context: dict | None = None,
        require_approval_resume: bool = False,
    ) -> dict:
        # Stable IDs are required for replay protection; arbitrary paths are not
        # accepted as IDs. Artifact-path construction belongs to the sandbox host.
        uuid.UUID(invocation_id)
        fingerprint = _digest(
            {"revision": revision, "inputs": inputs, "require_approval_resume": require_approval_resume}
        )
        existing = self.executions.get(invocation_id)
        if existing:
            if existing.fingerprint != fingerprint:
                raise ExecutionConflict("invocation already accepted with different input")
            return self.status(invocation_id)
        completed = self.completed.get(invocation_id)
        if completed:
            if completed[0] != fingerprint:
                raise ExecutionConflict("invocation already completed with different input")
            return self.status(invocation_id)
        active = sum(e.status not in self.TERMINAL for e in self.executions.values())
        if self.capacity != -1 and active >= self.capacity:
            raise ExecutionCapacityError("concurrency_limit_exceeded")
        if self.capacity != -1 and len(self.executions) >= self.capacity + self.max_pending_results:
            raise ExecutionCapacityError("result_delivery_backlog")
        template = self.workflows[revision][1]
        node_id = self.workflows[revision][2]
        if not isinstance(inputs, dict):
            raise ValueError("execution inputs must be an object")
        normalized = (
            deepcopy(inputs) if node_id else normalize_inputs_for_fields(inputs, start_node_input_fields(template))
        )
        execution = Execution(invocation_id, fingerprint, revision)
        execution.approvals = ApprovalBroker(
            lambda event: self._emit(execution, event), execution.stop, require_resume=require_approval_resume
        )
        ctx = {k: deepcopy(v) for k, v in (context or {}).items() if k in self.CONTEXT_KEYS}
        # Keep stable nested dictionaries: workflow branches receive shallow
        # context copies and must observe refreshed credentials after approval.
        for key in ("llm_credentials", "workflow_resources"):
            ctx.setdefault(key, {})
        execution.context = ctx
        execution.private_tokens.update(_credential_tokens(ctx))
        ctx.update(run_id=invocation_id, human_approval=execution.approvals, execution_budget_managed=True)
        # The immutable cached definition is reused; node instances and every
        # mutable graph/context object are constructed afresh for each request.
        workflow = Workflow(deepcopy(template))
        self.executions[invocation_id] = execution
        execution.task = asyncio.create_task(self._run(execution, workflow, normalized, ctx, node_id=node_id))
        return self.status(invocation_id)

    def status(self, invocation_id: str) -> dict:
        e = self.executions.get(invocation_id)
        if e is None:
            completed = self.completed.get(invocation_id)
            if completed is None:
                raise KeyError(invocation_id)
            return {
                "invocation_id": invocation_id,
                "generation": self.generation,
                "status": completed[1],
                "acknowledged": True,
            }
        return {
            "invocation_id": invocation_id,
            "generation": self.generation,
            "status": e.status,
            "seq": e.seq,
            "acknowledged_seq": e.acknowledged,
            "approvals": [a.public() for a in e.approvals.approvals.values() if a.reason is None],
        }

    async def _emit(self, e: Execution, event: dict) -> None:
        # Safe-call diagnostic bundles may contain the live context (including
        # credentials and circular references). They are never public telemetry.
        event = {key: value for key, value in event.items() if key not in {"args", "kwargs", "traceback"}}
        if "error_message" in event:
            event["error_message"] = _error_message(event["error_message"], e.private_tokens)
        # Freeze business inputs/outputs without changing their structure.
        event = json.loads(json.dumps(event, ensure_ascii=False, default=str, allow_nan=False))
        size = len(json.dumps(event, ensure_ascii=False).encode())
        if e.buffered_bytes + size > self.max_buffer_bytes and event.get("type") != "result":
            raise RuntimeError("execution_event_buffer_exceeded")
        async with e.changed:
            e.seq += 1
            frame = {**event, "invocation_id": e.invocation_id, "generation": self.generation, "seq": e.seq}
            e.events.append((frame, size))
            e.buffered_bytes += size
            if event.get("type") == "result":
                e.status = event["status"]
            elif event.get("type") == "approval_requested":
                e.status = "waiting_approval"
            elif event.get("type") == "approval_resolved":
                e.status = "waiting_approval" if e.approvals.waiting else "running"
            if event.get("type") == "node_event":
                node_id = event.get("node_id")
                node = self.workflows[e.revision][1].get(node_id, {})
                if node.get("node_type") != "HumanApprovalNode":
                    visit = event.get("span_id") or node_id
                    if visit and event.get("status") == "running":
                        e.active_nodes.add(visit)
                    elif visit and event.get("status") in {"success", "error"}:
                        e.active_nodes.discard(visit)
            # An approval pauses the workflow budget only while every live
            # branch is waiting. Other executing branches still consume time.
            paused = e.approvals.waiting and not e.active_nodes
            if paused and e.approval_wait_started is None:
                e.approval_wait_started = time.monotonic()
            elif not paused and e.approval_wait_started is not None:
                e.approval_wait_seconds += time.monotonic() - e.approval_wait_started
                e.approval_wait_started = None
            e.changed.notify_all()

    async def _enforce_budget(self, e: Execution, started: float, budget: float) -> None:
        while not e.stop.is_set():
            now = time.monotonic()
            paused = e.approval_wait_seconds
            if e.approval_wait_started is not None:
                paused += now - e.approval_wait_started
            remaining = budget - (now - started - paused)
            if remaining <= 0:
                e.timed_out = True
                e.stop.set()
                return
            try:
                await asyncio.wait_for(e.stop.wait(), timeout=min(remaining, 0.1))
            except TimeoutError:
                pass

    async def _run(self, e: Execution, workflow: Workflow, inputs: dict, context: dict, *, node_id=None) -> None:
        started = time.monotonic()
        watchdog = asyncio.create_task(self._enforce_budget(e, started, workflow._execution_timeout))
        result = {"final_outputs": {}, "error_dict": {}, "execution_time": 0.0}
        observed_errors = {}
        terminal_status = "failed"
        stream = None
        try:
            stream = (
                node_events(workflow, node_id, inputs, stop_event=e.stop, run_context=context)
                if node_id
                else workflow.astream(inputs, stop_event=e.stop, run_context=context)
            )
            async for event in stream:
                if event.get("status") == "finished":
                    result = {k: event.get(k) for k in ("final_outputs", "error_dict", "execution_time")}
                else:
                    if event.get("status") == "error":
                        observed_errors[event.get("node_id") or "__engine__"] = event.get(
                            "error_message", "execution_failed"
                        )
                    await self._emit(e, {**event, "type": "node_event"})
            result["error_dict"] = {**observed_errors, **(result["error_dict"] or {})}
            terminal_status = (
                "timed_out"
                if e.timed_out
                else "cancelled"
                if e.cancel_requested
                else "failed"
                if result["error_dict"]
                else "succeeded"
            )
        except asyncio.CancelledError:
            terminal_status = "timed_out" if e.timed_out else "cancelled"
        except Exception as exc:
            terminal_status = "failed"
            result["error_dict"] = {"__engine__": str(exc)}
        finally:
            if stream is not None:
                await stream.aclose()
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            if e.timed_out:
                result["error_dict"]["__engine__"] = "execution_timed_out"
            result["error_dict"] = {
                key: _error_message(value, e.private_tokens) for key, value in (result["error_dict"] or {}).items()
            }
            context.clear()
            e.private_tokens.clear()
            result["execution_time"] = time.monotonic() - started
            if len(json.dumps(result, ensure_ascii=False, default=str).encode()) > self.max_buffer_bytes:
                terminal_status = "failed"
                result = {
                    "final_outputs": {},
                    "error_dict": {"__engine__": "execution_result_too_large"},
                    "execution_time": time.monotonic() - started,
                }
            e.result = result
            await self._emit(e, {"type": "result", "status": terminal_status, **result})

    async def events(self, invocation_id: str, *, after: int = 0, wait_seconds: float = 0, limit: int = 100) -> dict:
        e = self.executions[invocation_id]
        if after < e.acknowledged or after > e.seq:
            raise ExecutionConflict("event cursor outside retained range")
        async with e.changed:
            if after == e.seq and e.status not in self.TERMINAL and wait_seconds > 0:
                try:
                    await asyncio.wait_for(e.changed.wait(), min(wait_seconds, 25))
                except TimeoutError:
                    pass
            frames = [frame for frame, _ in e.events if frame["seq"] > after][: max(1, min(limit, 1000))]
        return {**self.status(invocation_id), "events": frames}

    def acknowledge(self, invocation_id: str, through: int) -> None:
        e = self.executions.get(invocation_id)
        if e is None and invocation_id in self.completed:
            return
        if e is None:
            raise KeyError(invocation_id)
        if through > e.seq or through < 0:
            raise ExecutionConflict("invalid acknowledgement")
        e.acknowledged = max(e.acknowledged, through)
        while e.events and e.events[0][0]["seq"] <= through:
            _, size = e.events.popleft()
            e.buffered_bytes -= size
        if e.status in self.TERMINAL and through == e.seq and e.task.done():
            self.completed[invocation_id] = (e.fingerprint, e.status)
            del self.executions[invocation_id]

    async def decide(self, invocation_id: str, approval_id: str, approved: bool) -> dict:
        e = self.executions[invocation_id]
        if e.status in self.TERMINAL:
            raise ExecutionConflict("execution is terminal")
        return await e.approvals.decide(approval_id, approved)

    def resume(self, invocation_id: str, approval_id: str, context: dict) -> dict:
        e = self.executions[invocation_id]
        if e.status in self.TERMINAL:
            raise ExecutionConflict("execution is terminal")
        allowed = {"llm_credentials", "workflow_resources"}
        if not isinstance(context, dict) or set(context) != allowed:
            raise ValueError("resume requires refreshed credential and resource mappings")
        if any(not isinstance(value, dict) for value in context.values()):
            raise ValueError("resume context mappings must be objects")
        refreshed = deepcopy(context)

        def refresh():
            e.private_tokens.update(_credential_tokens(refreshed))
            for key, value in refreshed.items():
                e.context[key].clear()
                e.context[key].update(value)

        return e.approvals.resume(approval_id, refresh)

    async def cancel(self, invocation_id: str) -> dict:
        e = self.executions.get(invocation_id)
        if e is None or e.status in self.TERMINAL:
            return self.status(invocation_id)
        e.cancel_requested = True
        e.stop.set()
        await e.task
        return self.status(invocation_id)

    async def close(self) -> None:
        for e in self.executions.values():
            e.cancel_requested = True
            e.stop.set()
        await asyncio.gather(*(e.task for e in self.executions.values()), return_exceptions=True)
