"""Task-list filtering and SSE bridge ordering.

The HTTP layer is exercised at the repo seam (``TasksRepo.list_for_tenant``)
under a real tenant-scoped ``session_scope`` — same pattern as T11's
``test_tasks_routes_v2.py``, which keeps the suite asyncpg-friendly
(TestClient uses sync httpx, which can't drive async SSE generators).

The SSE generator is exercised directly with ``redis_url=None`` so the
no-Redis fallback path runs end-to-end against Postgres. That path is
the one that GUARANTEES strict ordering (BIGSERIAL on
``task_events.id``); the Redis path is best-effort latency optimization
on top.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


async def _seed_tenant_user(pg_engine, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Seed a tenant + a user under that tenant (RLS-bypass via superuser
    ``pg_engine`` — same fixture pattern as T11)."""
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
             "e": f"t13-{uuid.uuid4().hex[:6]}@example.com"},
        )


@pytest.mark.asyncio
async def test_list_tasks_filters_by_status_and_type(pg_engine):
    """``TasksRepo.list_for_tenant`` honours ``status`` + ``task_type``
    filters under tenant binding. The route layer is a thin pass-through
    so testing the repo is sufficient — `test_router_has_list_and_stream`
    below covers mount/wiring."""
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id)

    # Seed 3 tasks: 2 batch_exec (queued + finished), 1 scheduled_run (running).
    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        for st in ("queued", "finished"):
            task_id = uuid.uuid4()
            await repo.create(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_id=None,
                task_type="batch_exec",
                payload={},
                background_job_id=str(task_id),
            )
            await repo.update_status(task_id, status=st)
        scheduled_id = uuid.uuid4()
        await repo.create(
            task_id=scheduled_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="scheduled_run",
            payload={},
            background_job_id=str(scheduled_id),
        )
        await repo.update_status(scheduled_id, status="running")

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)

        # status filter — single value
        running, total = await repo.list_for_tenant(status=["running"])
        assert total == 1
        assert len(running) == 1 and running[0].status == "running"

        # task_type filter — single value, multi rows
        batch, total = await repo.list_for_tenant(task_type=["batch_exec"])
        assert total == 2
        assert len(batch) == 2
        assert {t.status for t in batch} == {"queued", "finished"}

        # Combined filters
        queued_batch, total = await repo.list_for_tenant(
            status=["queued"], task_type=["batch_exec"],
        )
        assert total == 1
        assert len(queued_batch) == 1
        assert queued_batch[0].status == "queued"

        # Empty filter lists == no filter (matches the route's
        # ``status or None`` collapse).
        all_three, total = await repo.list_for_tenant()
        assert total == 3
        assert len(all_three) == 3


@pytest.mark.asyncio
async def test_sse_emits_replay_after_last_event_id(pg_engine):
    """``Last-Event-ID`` resume + terminal-event close on the no-Redis
    fallback path.

    Seeds 5 ``progress`` events + 1 ``terminal`` event, asks the
    generator to resume after the 3rd event id, and verifies it emits
    exactly events 4, 5 and the terminal — then closes.
    """
    from vibecanvas_api.services.sse_bridge import task_event_stream
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id)

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
        for i in range(5):
            event_ids.append(await repo.insert_event(
                task_id, "progress", {"i": i}, tenant_id,
            ))
        event_ids.append(await repo.insert_event(
            task_id,
            "terminal",
            {"action": "task.finished"},
            tenant_id,
        ))

    last_seen = event_ids[2]   # resume after the 3rd progress event
    collected: list[str] = []
    gen = task_event_stream(
        task_id=task_id,
        last_event_id=last_seen,
        tenant_id=str(tenant_id),
        redis_url=None,            # force the DB-only fallback
    )
    async for frame in gen:
        collected.append(frame)
        if len(collected) >= 10:
            break  # safety bound — generator should self-terminate well before this

    # Expect exactly events 4, 5, and the terminal event (3 frames).
    assert len(collected) == 3, (
        f"Expected 3 frames; got {len(collected)}: {collected!r}"
    )
    # Frame format: each starts with `id: <int>`.
    for f in collected:
        assert f.startswith("id: "), f"bad frame prefix: {f!r}"
    # First two are progress events 4 and 5.
    assert "event: progress" in collected[0]
    assert "event: progress" in collected[1]
    # Strict ordering — ids are monotonically increasing.
    ids_emitted = [int(f.split("\n", 1)[0].split(": ")[1]) for f in collected]
    assert ids_emitted == sorted(ids_emitted)
    assert ids_emitted == event_ids[3:6]
    # Final frame closes on the terminal event.
    assert "event: terminal" in collected[-1]


def test_router_has_list_and_stream_routes():
    """Wiring smoke — confirms the new GET / and GET /{id}/stream
    endpoints are mounted on the same /api/v1/tasks router."""
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts
    app = build_app()
    paths = {r.path for r in application_route_contexts(app)}
    assert "/api/v1/tasks" in paths, (
        f"GET /api/v1/tasks (list) not mounted; got: {sorted(paths)}"
    )
    assert "/api/v1/tasks/{task_id}/stream" in paths, (
        f"GET /api/v1/tasks/{{task_id}}/stream not mounted; "
        f"got: {sorted(paths)}"
    )
