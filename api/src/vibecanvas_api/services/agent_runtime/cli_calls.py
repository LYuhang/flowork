"""Short RPC control for arbitrarily long, turn-owned CLI calls.

Transport deadlines apply to start/poll/cancel, never to the business task.
No automatic mutation retry. EOF/turn death/lease loss cancels pending work.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic

from vibecanvas_api.flowork_cli.cli import error, uncertain_result
from vibecanvas_api.services.agent_resources import context as agent_context

LEASE_SECONDS = 30.0


@dataclass
class Call:
    capability: object
    call_id: str
    operation: str
    task: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    lease: float = field(default_factory=monotonic)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=16))
    pending: dict | None = None
    sequence: int = 0
    hitl_id: str | None = None
    durable_lease: bool = False

    async def emit(self, value):
        await self.queue.put(value)


_calls: dict[tuple, Call] = {}


def key_for(capability, call_id):
    return (capability.tenant_id, capability.user_id, capability.chat_id,
            capability.turn_id, capability.runtime_session_id, call_id)


async def _work(call, token, arguments):
    try:
        if call.operation == "workflow.delete":
            from .cli_delete import execute
            result = await execute(call, arguments)
        elif call.operation.startswith("task."):
            from .cli_tasks import execute
            result = await execute(call, arguments)
        elif call.operation.startswith("deployment."):
            from .cli_deployments import execute
            result = await execute(call, arguments)
        elif call.operation in {"skill.create", "skill.update", "skill.check", "skill.download", "skill.delete"}:
            from .cli_skills import execute
            result = await execute(call, arguments)
        elif call.operation.startswith("knowledge."):
            from .cli_knowledge import execute
            result = await execute(call, arguments)
        else:
            from .cli_host import invoke_workflow_command
            result = await invoke_workflow_command(operation=call.operation, identity_token=token, arguments=arguments)
    except asyncio.CancelledError:
        raise
    except Exception:
        result = uncertain_result()
    await call.emit({"terminal": True, "result": result, "_operation": call.operation, "_arguments": arguments})


async def _stop(key):
    call = _calls.pop(key, None)
    if call is None:
        return
    if call.watchdog is not asyncio.current_task():
        call.watchdog.cancel()
    call.task.cancel()
    await asyncio.gather(call.task, return_exceptions=True)
    if call.durable_lease:
        from .cli_delete import release
        await release(call)
    return call.hitl_id


async def _watch(key, call):
    while key in _calls:
        await asyncio.sleep(2)
        if monotonic() - call.lease > LEASE_SECONDS:
            await _stop(key)
            return


async def cancel_turn_calls(tenant_id, chat_id, turn_id):
    keys = [key for key in _calls if key[0] == tenant_id and key[2:4] == (chat_id, turn_id)]
    await asyncio.gather(*(_stop(key) for key in keys))


async def command(capability, operation, arguments, token):
    key = key_for(capability, arguments["call_id"])
    if operation == "cli.cancel":
        hitl_id = await _stop(key)
        return {"cancelled": True, "_cli_events": ([{
            "event_type": "HITL_RESOLVED", "payload": {"hitl_request_id": hitl_id},
        }] if hitl_id else [])}
    # Refresh authorization before accepting or renewing a call. Cancel is
    # deliberately allowed after access/turn revocation.
    try:
        await agent_context.resolve_context(capability)
    except BaseException:
        await _stop(key)
        raise
    if operation == "cli.start":
        if key in _calls:
            return error("call_already_exists", "This CLI call already exists.", "Do not resubmit the command.")
        call = Call(capability, arguments["call_id"], arguments["operation"])
        _calls[key] = call
        call.task = asyncio.create_task(_work(call, token, arguments["arguments"]))
        call.watchdog = asyncio.create_task(_watch(key, call))
        return {"started": True}
    call = _calls.get(key)
    if call is None:
        return uncertain_result()
    call.lease = monotonic()
    if call.durable_lease:
        from .cli_delete import renew
        await renew(call)
    ack = arguments["ack"]
    if ack > call.sequence or ack < call.sequence - int(call.pending is not None):
        return error("invalid_ack", "Invalid CLI event acknowledgement.", "Do not reuse a CLI connection.")
    if call.pending is not None and ack == call.sequence:
        call.pending = None
    if call.pending is None:
        try:
            call.pending = call.queue.get_nowait()
            call.sequence += 1
        except asyncio.QueueEmpty:
            pass
    return {"sequence": call.sequence, "event": call.pending}
