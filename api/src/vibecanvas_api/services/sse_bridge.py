"""Task SSE: bounded durable replay, woken by commit notifications."""
from __future__ import annotations
import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from vibecanvas_api.services.state_notifications import task_changes

BUFFER_CAP = 1000
HEARTBEAT_SECONDS = 15.0
TERMINAL_EVENT_TYPES = {"terminal"}

def _sse_event(id_: int, event_type: str, payload: dict) -> str:
    """Format one Server-Sent Event frame.

    ``id:`` lets the EventSource client resume via ``Last-Event-ID``;
    ``event:`` lets handlers route by type; ``data:`` is the JSON
    payload. Trailing blank line ends the frame per the SSE spec.
    """
    return (
        f"id: {id_}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, default=str)}\n\n"
    )


def _payload_with_ts(payload: dict, ts) -> dict:
    out = dict(payload or {})
    if ts is not None:
        out["_event_ts"] = ts.isoformat()
    return out


async def _select_events_after(session, task_id: uuid.UUID, cursor: int) -> list:
    """Fetch all ``task_events`` rows with ``id > cursor`` for the task,
    ordered by id ASC. Writers serialize ID allocation per task."""
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    rows = await TasksRepo(session).events_for_task(
        task_id=task_id,
        after_seq=cursor,
        limit=BUFFER_CAP,
    )
    return [
        {
            "id": row.id,
            "event_type": row.event_type,
            "payload": row.payload,
            "ts": row.ts,
        }
        for row in rows
    ]


async def task_event_stream(
    *, task_id: uuid.UUID, last_event_id: int, tenant_id: str,
    authorization_guard: Callable[[], Awaitable[bool]] | None = None,
    authorization_check_seconds: float = 5.0,
):
    """Replay authorized rows; heartbeats and permission checks do not poll logs."""
    from vibecanvas_api.storage.db import session_scope
    loop = asyncio.get_running_loop()
    cursor = last_event_id
    next_authorization_check = 0.0
    next_heartbeat = loop.time() + HEARTBEAT_SECONDS
    needs_read = True
    async with task_changes(str(task_id)) as changed:
        while True:
            now = loop.time()
            if authorization_guard is not None and now >= next_authorization_check:
                if not await authorization_guard():
                    return
                next_authorization_check = now + max(1.0, authorization_check_seconds)
            if needs_read or changed.is_set():
                changed.clear()
                async with session_scope(tenant_id=tenant_id) as session:
                    rows = await _select_events_after(session, task_id, cursor)
                for row in rows:
                    cursor = row["id"]
                    yield _sse_event(cursor, row["event_type"],
                        _payload_with_ts(row["payload"], row["ts"]))
                    if row["event_type"] in TERMINAL_EVENT_TYPES:
                        return
                needs_read = len(rows) == BUFFER_CAP
                if needs_read or changed.is_set():
                    continue
            now = loop.time()
            if now >= next_heartbeat:
                yield ": heartbeat\n\n"
                next_heartbeat = loop.time() + HEARTBEAT_SECONDS
            deadline = next_heartbeat
            if authorization_guard is not None:
                deadline = min(deadline, next_authorization_check)
            try:
                await asyncio.wait_for(changed.wait(), max(0, deadline - loop.time()))
            except asyncio.TimeoutError:
                pass
