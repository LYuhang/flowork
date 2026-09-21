"""Strict SSE ordering without loss or duplication.

The ``sse_bridge.task_event_stream`` ordering guarantee comes from
``task_events.id`` BIGSERIAL + cursor-based resume. This file exercises
the bridge against pre-inserted events on the no-Redis path
(``redis_url=None``) — the realtime-pubsub race scenario needs a live
Redis and is deferred to staging.

Companion test: ``test_sse_emits_replay_after_last_event_id`` in
``test_tasks_list_and_sse.py`` covers a 6-event resume. This file extends
it with:
  1. A 100-event volume test — strict id-order over a larger batch where
     a counter wraparound or off-by-one would be visible.
  2. A clean resume-cursor test isolated from other concerns.
  3. A skip-marker for the Redis adversarial-timing variant (staging).
"""
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
        redis_url=None,
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
        redis_url=None,
    ):
        seen.append(_frame_id(frame))
        if len(seen) >= 20:
            break

    assert seen == event_ids[5:], (
        f"resume returned {seen}; expected {event_ids[5:]}"
    )


@pytest.mark.skip(
    reason="Needs Redis publish + concurrent writer/reader race — run in staging."
)
@pytest.mark.asyncio
async def test_sse_no_dup_under_redis_race():
    """G6b §3 — adversarial-timing variant covered in staging where
    Redis runs. The bridge's pubsub dedupe logic
    (``if ev['id'] <= cursor: continue``) guarantees no duplicates even
    when a SELECT-replay row and a pubsub message arrive for the same
    BIGSERIAL id, but exercising it needs a live broker."""
