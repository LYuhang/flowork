"""Task event replay and commit-driven live delivery."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


async def _seed_tenant_user(pg_engine, tenant_id: uuid.UUID,
                            user_id: uuid.UUID, marker: str) -> None:
    """Seed a tenant + a user under that tenant (RLS-bypass via the
    superuser ``pg_engine``)."""
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": tenant_id},
        )
        await c.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:u, :t, :e)"
            ),
            {"u": user_id, "t": tenant_id,
             "e": f"{marker}-{uuid.uuid4().hex[:6]}@example.com"},
        )


def _frame_id(frame: str) -> int:
    """Parse the ``id: <int>`` prefix off an SSE frame."""
    head = frame.split("\n", 1)[0]
    return int(head.split(": ", 1)[1])


@pytest.mark.asyncio
async def test_sse_strict_order_no_gaps(pg_engine):
    """G6b §1 — 100 events inserted in monotonic id order; bridge yields
    exactly those ids in order, no gaps, no dupes.

    BIGSERIAL guarantees monotonic insertion ids; the bridge's
    ``ORDER BY id`` SELECT-replay must reproduce them exactly.
    """
    from vibecanvas_api.services.sse_bridge import task_event_stream
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id, marker="g6b1")

    event_ids: list[int] = []
    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        await repo.create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="batch_exec",
            payload={},
            background_job_id=str(task_id),
        )
        await repo.update_status(task_id, status="running")
        for i in range(99):
            event_ids.append(await repo.insert_event(
                task_id,
                "result",
                {"i": i},
                tenant_id,
            ))
        event_ids.append(await repo.insert_event(
            task_id,
            "terminal",
            {"action": "task.finished"},
            tenant_id,
        ))

    collected_ids: list[int] = []
    async for frame in task_event_stream(
        task_id=task_id,
        last_event_id=0,
        tenant_id=str(tenant_id),
    ):
        collected_ids.append(_frame_id(frame))
        if len(collected_ids) >= 110:
            break  # safety bound — bridge should self-terminate at 100

    assert collected_ids == event_ids, (
        f"Expected exactly the {len(event_ids)} inserted BIGSERIAL ids in "
        f"order; got {len(collected_ids)}: head={collected_ids[:5]}, "
        f"tail={collected_ids[-5:]}"
    )
    # No duplicates.
    assert len(set(collected_ids)) == len(collected_ids), (
        "G6b dedup violated: duplicate ids in emitted stream"
    )
    # No gaps in chronological order (strictly increasing).
    assert all(
        b > a for a, b in zip(collected_ids, collected_ids[1:])
    ), "G6b ordering violated: ids not strictly increasing"


@pytest.mark.asyncio
async def test_sse_last_event_id_resume(pg_engine):
    """G6b §2 — resume with ``Last-Event-ID > 0`` returns only events
    past the cursor.

    Inserts 10 ``progress`` events + 1 terminal event; resumes
    after event #5 (1-indexed); expects events 6..10 + the terminal.
    """
    from vibecanvas_api.services.sse_bridge import task_event_stream
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id, marker="g6b2")

    event_ids: list[int] = []
    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        await repo.create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="batch_exec",
            payload={},
            background_job_id=str(task_id),
        )
        await repo.update_status(task_id, status="running")
        for i in range(10):
            event_ids.append(await repo.insert_event(
                task_id, "progress", {"i": i}, tenant_id,
            ))
        event_ids.append(await repo.insert_event(
            task_id,
            "terminal",
            {"action": "task.finished"},
            tenant_id,
        ))

    cursor = event_ids[4]  # resume after the 5th event (index 4)
    seen: list[int] = []
    async for frame in task_event_stream(
        task_id=task_id,
        last_event_id=cursor,
        tenant_id=str(tenant_id),
    ):
        seen.append(_frame_id(frame))
        if len(seen) >= 20:
            break

    assert seen == event_ids[5:], (
        f"resume returned {seen}; expected {event_ids[5:]}"
    )


@pytest.mark.asyncio
async def test_sse_live_commit_wakes_idle_reader(pg_engine, monkeypatch):
    import asyncio
    from vibecanvas_api.services import sse_bridge, state_notifications
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    monkeypatch.setenv('STATE_NOTIFICATION_DATABASE_URL',
        pg_engine.url.render_as_string(hide_password=False))
    tenant, user, task = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant, user, 'live')
    async with session_scope(tenant_id=str(tenant)) as session:
        await TasksRepo(session).create(task_id=task, tenant_id=tenant,
            user_id=user, workflow_id=None, task_type='batch_exec',
            payload={}, background_job_id=str(task))
    reads = 0
    original = sse_bridge._select_events_after
    async def read(*args):
        nonlocal reads
        reads += 1
        return await original(*args)
    monkeypatch.setattr(sse_bridge, '_select_events_after', read)
    stream = sse_bridge.task_event_stream(task_id=task, last_event_id=0, tenant_id=str(tenant))
    pending = asyncio.create_task(anext(stream))
    try:
        # Allow initial connection/reconciliation, then prove idle does not poll.
        await asyncio.sleep(0.5)
        count = reads
        await asyncio.sleep(0.5)
        assert reads == count and not pending.done()
        async with session_scope(tenant_id=str(tenant)) as session:
            event_id = await TasksRepo(session).insert_event(task, 'terminal',
                {'action': 'task.finished'}, tenant)
            await asyncio.sleep(0.1)
            assert not pending.done(), 'uncommitted event must not be delivered'
        frame = await asyncio.wait_for(pending, 3)
        assert _frame_id(frame) == event_id
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
    assert not state_notifications._listeners


@pytest.mark.asyncio
async def test_concurrent_event_writer_waits_for_previous_commit(pg_engine):
    import asyncio
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant, user, task = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant, user, 'ordered')
    async with session_scope(tenant_id=str(tenant)) as session:
        await TasksRepo(session).create(task_id=task, tenant_id=tenant,
            user_id=user, workflow_id=None, task_type='batch_exec',
            payload={}, background_job_id=str(task))
    entered = asyncio.Event()
    async def second_writer():
        async with session_scope(tenant_id=str(tenant)) as session:
            entered.set()
            return await TasksRepo(session).insert_event(task, 'progress', {'i': 2}, tenant)
    pending = None
    try:
        async with session_scope(tenant_id=str(tenant)) as session:
            first = await TasksRepo(session).insert_event(task, 'progress', {'i': 1}, tenant)
            pending = asyncio.create_task(second_writer())
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.sleep(0.2)
            assert not pending.done(), 'second writer must wait for first commit'
        second = await asyncio.wait_for(pending, 3)
        assert second > first
        async with session_scope(tenant_id=str(tenant)) as session:
            rows = await TasksRepo(session).events_for_task(task_id=task, after_seq=0)
            assert [row.id for row in rows] == [first, second]
            assert [row.payload['i'] for row in rows] == [1, 2]
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
