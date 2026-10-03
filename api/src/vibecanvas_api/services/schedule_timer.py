"""Deadline-driven schedule dispatcher; PostgreSQL notifications invalidate waits.

The database remains authoritative. Notifications carry no resource data and are
only wakeups: startup, reconnect and periodic reconciliation recover missed ones.
Every worker may run this loop; row locks and run keys arbitrate dispatch.
"""
from __future__ import annotations

import asyncio
import select
import socket
import threading
from contextlib import closing

import psycopg
import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url

from vibecanvas_api.storage.db import maintenance_database_url
from vibecanvas_api.storage.sync_session import short_admin_session

logger = structlog.get_logger(__name__)
CHANNEL = "flowork_schedule_changed"
RECONCILE_SECONDS = 30.0


async def dispatch_and_wait_seconds() -> float:
    from vibecanvas_api.background_tasks.scheduled_runs import _dispatch_due_scheduled_runs
    await _dispatch_due_scheduled_runs()
    async with short_admin_session() as session:
        seconds = (await session.execute(text("""
            SELECT extract(epoch FROM (min(next_run_at) - clock_timestamp()))
            FROM task_schedules WHERE enabled AND next_run_at IS NOT NULL
            AND (end_at IS NULL OR end_at > clock_timestamp())
        """))).scalar_one_or_none()
    # A different dispatcher can briefly own an already-due row. Avoid spinning.
    return RECONCILE_SECONDS if seconds is None else min(RECONCILE_SECONDS, max(0.05, float(seconds)))


class ScheduleTimer:
    def __init__(self):
        self.stopped = threading.Event()
        self.reader, self.writer = socket.socketpair()
        self.thread = threading.Thread(target=self._run, name="schedule-timer", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.writer.send(b"x")
        self.thread.join(timeout=35)
        if self.thread.is_alive():
            logger.warning("schedule_timer_shutdown_pending")
        else:
            self.reader.close()
            self.writer.close()

    def _run(self):
        # Dedicated session connection: LISTEN must not use transaction pooling.
        url = make_url(maintenance_database_url()).set(drivername="postgresql")
        while not self.stopped.is_set():
            try:
                with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True,
                                     connect_timeout=5) as connection:
                    connection.execute(f"LISTEN {CHANNEL}")
                    while not self.stopped.is_set():
                        # Drain before querying, so changes committed during or after
                        # that query remain readable and interrupt the subsequent wait.
                        with closing(connection.notifies(timeout=0)) as notifications:
                            for _ in notifications:
                                pass
                        timeout = asyncio.run(dispatch_and_wait_seconds())
                        select.select([connection, self.reader], [], [], timeout)
            except Exception:
                # Do not log a connection exception: it may include credentials.
                logger.warning("schedule_timer_reconnecting")
                self.stopped.wait(1)
