"""Task detail and cancellation-route branching.

See the Task lifecycle in ``docs/architecture.md``.

Approach: seed tasks rows via the superuser ``pg_engine`` (RLS-bypass)
into the schema migrated by the autouse ``_migrate`` fixture, then
exercise the route's branching logic through:

* the ``TasksRepo`` directly (where the route's UPDATE / insert_event
  semantics live) — this confirms the durable side-effects;
* a module-level smoke import that catches load-time syntax / wiring
  errors;
* an OpenAPI route-table check that confirms the router is mounted.

The route's HTTP-layer behaviours (404 on missing, 409 on terminal)
fall directly out of the ``TasksRepo.get`` result + the status check;
they are exercised here at the repo seam rather than via a TestClient
to keep the test suite asyncpg-friendly (TestClient uses sync httpx).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text


def test_router_mounted_in_app():
    """The /tasks router is registered with GET + cancel endpoints."""
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts
    app = build_app()
    paths = {r.path for r in application_route_contexts(app)}
    assert "/api/v1/tasks/{task_id}" in paths
    assert "/api/v1/tasks/{task_id}/cancel" in paths


@pytest.mark.asyncio
async def test_task_to_out_serializes_uuid_and_datetime():
    """``_task_to_out`` stringifies UUIDs and ISO-encodes datetimes."""
    from datetime import datetime, timezone
    from vibecanvas_api.authorization.types import Action, Decision
    from vibecanvas_api.routes.tasks import _task_to_out

    class _Stub:
        id = uuid.uuid4()
        status = "queued"
        progress = 0.0
        task_type = "batch_exec"
        workflow_id = "wf_abc"
        payload = {"k": "v"}
        result = None
        results_uri = None
        error = None
        background_job_id = "cid"
        submitted_at = datetime(2026, 5, 23, tzinfo=timezone.utc)
        started_at = None
        finished_at = None
        user_id = uuid.uuid4()

    class _Provenance:
        async def build(self, **_kwargs):
            from vibecanvas_api.schemas.access import (
                ResourcePartyOut,
                ResourceProvenanceOut,
            )

            return ResourceProvenanceOut(
                ownership_scope="personal",
                origin_type="created",
                owner=ResourcePartyOut(
                    type="user",
                    display_name="Task owner",
                ),
            )

    out = await _task_to_out(
        _Stub(),
        Decision(
            True,
            capabilities=frozenset({Action.VIEW}),
            effective_role="viewer",
        ),
        _Provenance(),
    )
    assert out["id"] == str(_Stub.id)
    assert out["status"] == "queued"
    assert out["submitted_at"].startswith("2026-05-23T")
    assert out["started_at"] is None
    assert out["finished_at"] is None
    assert out["access"]["capabilities"] == ["view"]


def test_cancel_body_drops_unknown_fields():
    """``CancelBody`` ignores fields it doesn't know about."""
    from vibecanvas_api.routes.tasks import CancelBody
    b = CancelBody.model_validate({"mode": "force", "evil": "ignored"})
    assert b.mode == "force"
    assert b.model_dump() == {"mode": "force"}
    # Default mode is "soft" when absent.
    assert CancelBody.model_validate({}).mode == "soft"


async def _seed_tenant_user(pg_engine, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
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
            {"u": user_id, "t": tenant_id, "e": f"u-{uuid.uuid4().hex[:6]}@example.com"},
        )


async def _seed_task(
    pg_engine,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    status: str,
    finished_at: bool = False,
) -> uuid.UUID:
    from datetime import datetime, timezone

    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    task_id = uuid.uuid4()
    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = TasksRepo(session)
        await repo.create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="batch_exec",
            payload={},
            background_job_id=str(task_id),
        )
        await repo.update_status(
            task_id,
            status=status,
            **({"finished_at": datetime.now(timezone.utc)} if finished_at else {}),
        )
    return task_id


@pytest.mark.asyncio
async def test_get_returns_row_for_caller_tenant(pg_engine):
    """``TasksRepo.get`` returns the task row when called within a
    tenant-scoped session matching the row's tenant_id."""
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id)
    task_id = await _seed_task(
        pg_engine, tenant_id=tenant_id, user_id=user_id, status="queued",
    )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        t = await TasksRepo(s).get(task_id)
        assert t is not None
        assert t.status == "queued"
        assert str(t.id) == str(task_id)


@pytest.mark.asyncio
async def test_get_returns_none_for_foreign_tenant(pg_engine):
    """Cross-tenant lookup returns ``None`` (RLS-filtered), which the
    route translates to a 404. This guards against existence-leak."""
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    owner_tenant, owner_user = uuid.uuid4(), uuid.uuid4()
    foreign_tenant, foreign_user = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, owner_tenant, owner_user)
    await _seed_tenant_user(pg_engine, foreign_tenant, foreign_user)
    task_id = await _seed_task(
        pg_engine, tenant_id=owner_tenant, user_id=owner_user, status="queued",
    )

    # Looking up the owner's task as the foreign tenant: RLS hides it.
    async with session_scope(tenant_id=str(foreign_tenant)) as s:
        t = await TasksRepo(s).get(task_id)
        assert t is None


@pytest.mark.asyncio
async def test_cancel_queued_flips_status_and_emits_event(pg_engine):
    """``queued`` cancel path: UPDATE → ``cancelled``, ``finished_at=now()``,
    plus a ``task_events`` row with ``reason=queued-cancel``.

    Exercises the exact SQL the route runs."""
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id)
    task_id = await _seed_task(
        pg_engine, tenant_id=tenant_id, user_id=user_id, status="queued",
    )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        await s.execute(
            text(
                "UPDATE tasks SET status='cancelled', finished_at=now() "
                "WHERE id=:id"
            ),
            {"id": task_id},
        )
        await repo.insert_event(
            task_id, "terminal",
            {
                "action": "task.cancelled",
                "task_status": "cancelled",
                "data": {"reason": "queued-cancel"},
            },
            tenant_id,
        )
        await s.commit()

    # Re-read via a fresh session — confirm durable side-effects.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        t = await TasksRepo(s).get(task_id)
        assert t.status == "cancelled"
        assert t.finished_at is not None
        events = await TasksRepo(s).events_for_task(task_id=task_id)
        assert events, "expected at least one task_events row"
        assert events[-1].event_type == "terminal"
        assert events[-1].payload["action"] == "task.cancelled"
        assert events[-1].payload["data"] == {"reason": "queued-cancel"}


@pytest.mark.asyncio
async def test_task_event_history_supports_time_order_and_stable_sequence_cursor(pg_engine):
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id)
    task_id = await _seed_task(
        pg_engine, tenant_id=tenant_id, user_id=user_id, status="running",
    )
    base = datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc)
    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = TasksRepo(session)
        ids = [
            await repo.insert_event(task_id, "log", {"message": f"event-{index}"}, tenant_id)
            for index in range(3)
        ]
        for index, event_id in enumerate(ids):
            await session.execute(
                text("UPDATE task_events SET ts=:ts WHERE id=:id"),
                {"id": event_id, "ts": base + timedelta(minutes=index)},
            )
        await session.commit()

    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = TasksRepo(session)
        page = await repo.events_for_task(
            task_id=task_id,
            from_=base + timedelta(seconds=30),
            to=base + timedelta(minutes=3),
            limit=2,
            descending=True,
        )
        assert [event.id for event in page] == [ids[2], ids[1]]
        assert await repo.latest_event_seq(task_id) == ids[2]
        next_page = await repo.events_for_task(
            task_id=task_id,
            before_seq=page[-1].id,
            limit=2,
            descending=True,
        )
        assert [event.id for event in next_page] == [ids[0]]
        ascending = await repo.events_for_task(
            task_id=task_id,
            limit=2,
            descending=False,
        )
        assert [event.id for event in ascending] == [ids[0], ids[1]]
        ascending_next = await repo.events_for_task(
            task_id=task_id,
            after_seq=ascending[-1].id,
            limit=2,
            descending=False,
        )
        assert [event.id for event in ascending_next] == [ids[2]]


@pytest.mark.asyncio
async def test_cancel_running_flips_to_cancelling_with_mode(pg_engine):
    """``running`` cancel path: UPDATE → ``cancelling``, ``task_events``
    row carries ``mode``. ``finished_at`` is NOT set yet (the worker
    sets it when it actually finishes)."""
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id)
    task_id = await _seed_task(
        pg_engine, tenant_id=tenant_id, user_id=user_id, status="running",
    )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        await s.execute(
            text("UPDATE tasks SET status='cancelling' WHERE id=:id"),
            {"id": task_id},
        )
        await repo.insert_event(
            task_id, "state",
            {
                "action": "task.cancel_requested",
                "task_status": "cancelling",
                "data": {"reason": "cancel-requested", "mode": "force"},
            },
            tenant_id,
        )
        await s.commit()

    async with session_scope(tenant_id=str(tenant_id)) as s:
        t = await TasksRepo(s).get(task_id)
        assert t.status == "cancelling"
        assert t.finished_at is None
        events = await TasksRepo(s).events_for_task(task_id=task_id)
        assert events and events[-1].payload["action"] == "task.cancel_requested"
        assert events[-1].payload["data"]["mode"] == "force"
        assert events[-1].payload["data"]["reason"] == "cancel-requested"


@pytest.mark.parametrize("terminal_status", ["finished", "failed",
                                              "cancelling", "cancelled"])
@pytest.mark.asyncio
async def test_terminal_status_triggers_409_branch(pg_engine, terminal_status):
    """The route's ``status in {finished, failed, cancelling, cancelled}``
    branch matches every value in the CHECK-constraint's terminal set.

    Confirming the seeded row's status is in the route's 409 set is the
    branch condition the route handler evaluates before raising."""
    from vibecanvas_api.routes.tasks import _TERMINAL_OR_INFLIGHT_CANCEL
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id)
    task_id = await _seed_task(
        pg_engine, tenant_id=tenant_id, user_id=user_id,
        status=terminal_status, finished_at=terminal_status in ("finished", "failed"),
    )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        t = await TasksRepo(s).get(task_id)
        assert t.status == terminal_status
        assert t.status in _TERMINAL_OR_INFLIGHT_CANCEL


@pytest.mark.asyncio
async def test_missing_task_id_branch(pg_engine):
    """A non-existent task_id returns ``None`` from ``TasksRepo.get``,
    which the route translates to a 404."""
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id)
    bogus_id = uuid.uuid4()

    async with session_scope(tenant_id=str(tenant_id)) as s:
        t = await TasksRepo(s).get(bogus_id)
        assert t is None
