"""Shared commit notifications for state observers across processes.

One LISTEN connection per channel and event loop, not per request.

Notifications contain only resource IDs. They never authorize access or carry
results: subscribers re-read committed state using their tenant-bound session.
Reconnect wakes subscribers to recover events missed during disconnection.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os

import asyncpg
from sqlalchemy.engine import make_url
import structlog

from vibecanvas_api.config import config

logger = structlog.get_logger(__name__)
CHANNEL = 'flowork_execution_state'
_listeners: dict[tuple[asyncio.AbstractEventLoop, str], '_Listener'] = {}


class _Listener:
    def __init__(self, channel: str):
        self.channel = channel
        self.subscribers: dict[str, set[asyncio.Event]] = {}
        self.task = asyncio.create_task(self.listen())

    def wake(self, resource_id: str | None = None):
        groups = self.subscribers.values() if resource_id is None else [self.subscribers.get(resource_id, ())]
        for events in groups:
            for event in events:
                event.set()

    async def listen(self):
        # LISTEN requires a session connection, not PgBouncer transaction mode.
        url = make_url(os.environ.get('STATE_NOTIFICATION_DATABASE_URL') or config.database.url)
        dsn = url.set(drivername='postgresql').render_as_string(hide_password=False)
        while True:
            try:
                connection = await asyncpg.connect(
                    dsn, timeout=5, command_timeout=5,
                    server_settings={"application_name": "flowork-state-listener"},
                )
                try:
                    disconnected = asyncio.Event()
                    connection.add_termination_listener(lambda _: disconnected.set())
                    await connection.add_listener(
                        self.channel, lambda _conn, _pid, _channel, payload: self.wake(payload),
                    )
                    self.wake()  # Also closes the initial subscribe/read race.
                    await disconnected.wait()
                finally:
                    await connection.close(timeout=5)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log a DSN-bearing exception. Retry only the connection;
                # execution state is not periodically queried during outages.
                logger.warning('state_notification_reconnecting')
                await asyncio.sleep(1)


@asynccontextmanager
async def state_changes(channel: str, resource_id: str):
    key = (asyncio.get_running_loop(), channel)
    listener = _listeners.get(key)
    if listener is None:
        listener = _Listener(channel)
        _listeners[key] = listener
    event = asyncio.Event()
    listener.subscribers.setdefault(resource_id, set()).add(event)
    try:
        yield event
    finally:
        group = listener.subscribers[resource_id]
        group.remove(event)
        if not group:
            del listener.subscribers[resource_id]
        if not listener.subscribers:
            del _listeners[key]
            listener.task.cancel()
            await asyncio.gather(listener.task, return_exceptions=True)


@asynccontextmanager
async def execution_changes(execution_id: str):
    async with state_changes(CHANNEL, execution_id) as changed:
        yield changed


@asynccontextmanager
async def agent_changes(run_id: str):
    async with state_changes('flowork_agent_state', run_id) as changed:
        yield changed


@asynccontextmanager
async def agent_commands(run_id: str):
    async with state_changes('flowork_agent_command', run_id) as changed:
        yield changed


@asynccontextmanager
async def task_changes(task_id: str):
    async with state_changes('flowork_task_state', task_id) as changed:
        yield changed


async def invalidations(channel, resource_id, authorized, *, heartbeat_seconds=15.0, authorization_seconds=5.0):
    """Broadcast invalidations, including on initial connection and reconnect.

    Consumers fetch authoritative snapshots; this stream carries no history
    or cursor and never substitutes for the durable message event stream.
    """
    loop = asyncio.get_running_loop()
    next_auth = 0.0
    next_heartbeat = loop.time() + heartbeat_seconds
    async with state_changes(channel, resource_id) as changed:
        changed.set()
        while True:
            now = loop.time()
            if now >= next_auth:
                if not await authorized():
                    return
                next_auth = now + authorization_seconds
            if changed.is_set():
                changed.clear()
                yield True
            now = loop.time()
            if now >= next_heartbeat:
                yield False
                next_heartbeat = loop.time() + heartbeat_seconds
            try:
                await asyncio.wait_for(changed.wait(), max(0, min(next_auth, next_heartbeat) - loop.time()))
            except asyncio.TimeoutError:
                pass


async def event_batches(channel, resource_id, read, authorized, *, heartbeat_seconds=15.0, authorization_seconds=5.0):
    """Yield durable pages (or None for heartbeat), with no idle event queries.

    `read` must return a bounded page and advance its cursor as the consumer
    handles it. Notifications racing a read remain set for the next pass.
    """
    loop = asyncio.get_running_loop()
    next_auth = 0.0
    next_heartbeat = loop.time() + heartbeat_seconds
    needs_read = True
    async with state_changes(channel, resource_id) as changed:
        while True:
            now = loop.time()
            if now >= next_auth:
                if not await authorized():
                    return
                next_auth = now + authorization_seconds
            if needs_read or changed.is_set():
                changed.clear()
                rows = await read()
                needs_read = bool(rows)
                if rows:
                    yield rows
                    continue
            now = loop.time()
            if now >= next_heartbeat:
                yield None
                next_heartbeat = loop.time() + heartbeat_seconds
            try:
                await asyncio.wait_for(changed.wait(), max(0, min(next_auth, next_heartbeat) - loop.time()))
            except asyncio.TimeoutError:
                pass
