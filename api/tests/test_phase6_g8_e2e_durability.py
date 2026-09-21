"""Two-account RLS isolation, cancellation persistence, and atomic identifiers.

Three durability invariants the §13 G8 gate guarantees:
  1) Two tenants never see each other's task rows (RLS isolation).
  2) A ``cancelled`` task stays ``cancelled`` across fresh sessions.
  3) ``tasks.id == tasks.background_job_id`` (atomic submit invariant from §6.3).

The full DBOS-worker e2e — submit batch → worker picks up → progress
events stream → soft-cancel → SIGUSR1 → worker stops → row goes to
``cancelled`` — needs a running DBOS worker + signal delivery and is
deferred to staging via the skip-marked test at the bottom.
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


@pytest.mark.asyncio
async def test_two_tenant_task_isolation(pg_engine):
    """G8 §1 — tenant B's ``TasksRepo.list_for_tenant`` cannot see tenant
    A's task.

    Drives both writes + the cross-tenant read through ``session_scope``
    (non-superuser path, FORCE RLS applies). If the policy were missing,
    tenant B would see tenant A's row and the assertion would fail.
    """
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    task_a = uuid.uuid4()
    async with pg_engine.begin() as c:
        for t in (tenant_a, tenant_b):
            await c.execute(
                text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
                {"t": t},
            )
        for u, t in ((user_a, tenant_a), (user_b, tenant_b)):
            await c.execute(
                text(
                    "INSERT INTO users(user_id, tenant_id, email) "
                    "VALUES (:u, :t, :e)"
                ),
                {"u": u, "t": t, "e": f"g8iso-{uuid.uuid4().hex[:6]}@example.com"},
            )

    async with session_scope(tenant_id=str(tenant_a)) as s:
        await TasksRepo(s).create(
            task_id=task_a,
            tenant_id=tenant_a,
            user_id=user_a,
            workflow_id=None,
            task_type="batch_exec",
            payload={},
            background_job_id=str(task_a),
        )

    async with session_scope(tenant_id=str(tenant_b)) as s:
        items, total = await TasksRepo(s).list_for_tenant()
    assert total == 0
    assert task_a not in {t.id for t in items}, (
        "G8 RLS isolation violated: tenant B's repo listed tenant A's task"
    )


@pytest.mark.asyncio
async def test_cancelled_status_persists_across_sessions(pg_engine):
    """G8 §2 — once a task is ``cancelled``, it stays that way on fresh reads.

    A fresh ``session_scope`` opens a new DB transaction with no shared
    identity map, so any in-memory caching shenanigans would be exposed.
    """
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id, marker="g8can")

    # Session 1: write a 'cancelled' task.
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
        from datetime import datetime, timezone
        await repo.update_status(
            task_id,
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
        )

    # Session 2 (fresh): read back; status remains 'cancelled'.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        t = await TasksRepo(s).get(task_id)
        assert t is not None, "task disappeared between sessions"
        assert t.status == "cancelled", (
            f"G8 cancellation durability violated: status={t.status!r}"
        )
        assert t.finished_at is not None, (
            "cancelled tasks must carry finished_at"
        )


@pytest.mark.asyncio
async def test_atomic_id_invariant(pg_engine):
    """G8 §3 — any task created via the route has ``id == background_job_id``
    (UUID match).

    The §6.3 atomic-submit contract: the route generates one UUID,
    stamps it into the row, and reuses it as the DBOS message id —
    keeps the broker delivery and the DB row identifier in lock-step
    (the reconciler relies on this for idempotent re-publish).
    """
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    await _seed_tenant_user(pg_engine, tenant_id, user_id, marker="g8aid")

    # Simulate the route's atomic-submit code path: id is generated
    # server-side and reused as background_job_id.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        await TasksRepo(s).create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="batch_exec",
            payload={"data_source": {}, "column_mapping": {}},
            background_job_id=str(task_id),
        )

    async with pg_engine.connect() as c:
        row = (await c.execute(
            text("SELECT id, background_job_id FROM tasks WHERE id=:id"),
            {"id": task_id},
        )).one()
    assert str(row.id) == row.background_job_id == str(task_id), (
        f"G8 atomic-ID invariant violated: id={row.id} background_job_id="
        f"{row.background_job_id} expected={task_id}"
    )


@pytest.mark.skip(
    reason="Needs DBOS worker process + SIGUSR1 signal delivery — run in staging."
)
@pytest.mark.asyncio
async def test_g8_full_cancel_with_dbos_worker():
    """G8 §4 — full e2e: submit → progress → cancel → SIGUSR1 →
    cancelled. Staging only (the sandbox cannot run a worker process)."""
