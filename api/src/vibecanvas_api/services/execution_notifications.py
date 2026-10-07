"""One LISTEN connection per active API loop, shared by all HTTP observers.

Notifications contain only execution IDs. They never authorize access or carry
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
_listeners: dict[asyncio.AbstractEventLoop, '_Listener'] = {}


class _Listener:
    def __init__(self):
        self.subscribers: dict[str, set[asyncio.Event]] = {}
        self.task = asyncio.create_task(self.listen())

    def wake(self, execution_id: str | None = None):
        groups = self.subscribers.values() if execution_id is None else [self.subscribers.get(execution_id, ())]
        for events in groups:
            for event in events:
                event.set()

    async def listen(self):
        # LISTEN requires a session connection, not PgBouncer transaction mode.
        url = make_url(os.environ.get('EXECUTION_NOTIFICATION_DATABASE_URL') or config.database.url)
        dsn = url.set(drivername='postgresql').render_as_string(hide_password=False)
        while True:
            try:
                connection = await asyncpg.connect(
                    dsn, timeout=5, command_timeout=5,
                    server_settings={"application_name": "flowork-execution-listener"},
                )
                try:
                    disconnected = asyncio.Event()
                    connection.add_termination_listener(lambda _: disconnected.set())
                    await connection.add_listener(
                        CHANNEL, lambda _conn, _pid, _channel, payload: self.wake(payload),
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
                logger.warning('execution_notification_reconnecting')
                await asyncio.sleep(1)


@asynccontextmanager
async def execution_changes(execution_id: str):
    loop = asyncio.get_running_loop()
    listener = _listeners.get(loop)
    if listener is None:
        listener = _Listener()
        _listeners[loop] = listener
    event = asyncio.Event()
    listener.subscribers.setdefault(execution_id, set()).add(event)
    try:
        yield event
    finally:
        group = listener.subscribers[execution_id]
        group.remove(event)
        if not group:
            del listener.subscribers[execution_id]
        if not listener.subscribers:
            del _listeners[loop]
            listener.task.cancel()
            await asyncio.gather(listener.task, return_exceptions=True)
