import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text

from tests.test_scheduled_runs import _seed_tenant_user_workflow
from vibecanvas_api.background_tasks import scheduled_runs as worker
from vibecanvas_api.services.schedule_timer import CHANNEL, dispatch_and_wait_seconds
from vibecanvas_api.storage import db as db_mod
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_tasks import TasksRepo


@pytest.mark.asyncio
async def test_deadline_dispatch_keeps_cadence_and_skips_missed_slots(pg_engine, monkeypatch):
    tenant, user, wf = await _seed_tenant_user_workflow(pg_engine)
    task_id, schedule_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    due = now - timedelta(seconds=32)
    async with session_scope(tenant_id=str(tenant)) as session:
        await TasksRepo(session).create_schedule(task_id=task_id, schedule_id=schedule_id,
            tenant_id=tenant, user_id=user, workflow_id=wf, name="timer", enabled=True,
            schedule_type="interval", cron_expr=None, interval_seconds=15, timezone="UTC",
            input_preset={}, mount_enabled=False, notification_policy={}, next_run_at=due)
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)
    monkeypatch.setattr(worker, "utc_now", lambda: now)
    sent = []
    async def enqueue(session, name, **kw):
        sent.append(kw)
    monkeypatch.setattr(worker, "enqueue_background_job_in_transaction", enqueue)
    await asyncio.gather(worker._dispatch_due_scheduled_runs(), worker._dispatch_due_scheduled_runs())
    wait = await dispatch_and_wait_seconds()
    assert 0 < wait <= 13
    await worker._dispatch_due_scheduled_runs()
    assert len(sent) == 1
    async with session_scope(tenant_id=str(tenant)) as session:
        schedule = await TasksRepo(session).get_schedule(schedule_id)
        assert schedule.next_run_at == due + timedelta(seconds=45)
        await TasksRepo(session).update_schedule(schedule_id, enabled=False)
    assert await dispatch_and_wait_seconds() == 30


@pytest.mark.asyncio
async def test_schedule_notification_is_delivered_only_after_commit(pg_engine):
    tenant, user, wf = await _seed_tenant_user_workflow(pg_engine)
    url = pg_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(url, autocommit=True) as listener:
        listener.execute(f"LISTEN {CHANNEL}")
        def notifications():
            with closing(listener.notifies(timeout=0.1)) as stream:
                return list(stream)
        async with session_scope(tenant_id=str(tenant)) as session:
            await TasksRepo(session).create_schedule(task_id=uuid4(), schedule_id=uuid4(),
                tenant_id=tenant, user_id=user, workflow_id=wf, name="notify", enabled=False,
                schedule_type="interval", cron_expr=None, interval_seconds=5, timezone="UTC",
                input_preset={}, mount_enabled=False, notification_policy={}, next_run_at=None)
            assert not await asyncio.to_thread(notifications)
        events = await asyncio.to_thread(notifications)
        assert len(events) == 1 and events[0].channel == CHANNEL and events[0].payload == ""
        async with pg_engine.connect() as conn:
            transaction = await conn.begin()
            await conn.execute(text("UPDATE task_schedules SET enabled=true"))
            await transaction.rollback()
        assert not await asyncio.to_thread(notifications)
