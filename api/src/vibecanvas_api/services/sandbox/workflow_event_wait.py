"""Wait for runtime frames or a committed host command without database polling."""
from __future__ import annotations

import asyncio


async def wait_for_execution_event(client, execution_id: str, after: int, changed: asyncio.Event):
    """Return frames plus whether persisted host commands need reconciliation.

    The bounded runtime wait is a transport heartbeat, not a database poll.
    Cancellation closes the RPC connection through WorkflowRpcClient.call.
    """
    if changed.is_set():
        changed.clear()
        return [], True
    frames_task = asyncio.create_task(client.call(
        "events", invocation_id=execution_id, after=after,
        wait_seconds=25, limit=100, timeout=30,
    ))
    command_task = asyncio.create_task(changed.wait())
    try:
        await asyncio.wait((frames_task, command_task), return_when=asyncio.FIRST_COMPLETED)
        reconcile = changed.is_set()
        if reconcile:
            changed.clear()
        frames = frames_task.result()["events"] if frames_task.done() else []
        return frames, reconcile
    finally:
        for task in (frames_task, command_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(frames_task, command_task, return_exceptions=True)
