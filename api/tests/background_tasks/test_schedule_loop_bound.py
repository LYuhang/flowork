"""Periodic DBOS schedule tasks must survive two ticks.

A DBOS worker is a single long-lived process. Each schedule
tick runs a task whose sync body does ``asyncio.run(_async_body())``.
``asyncio.run`` creates a fresh event loop, runs the coroutine, then
CLOSES that loop. If the async body acquires a *process-global pooled*
async engine (``db.get_admin_engine`` / ``tenant_db.session_scope_admin``
which builds + caches one engine for the whole process), the first tick
binds that engine's asyncpg pool to loop #1 and then closes loop #1.
The second tick gets a brand-new loop but the cached global engine still
holds connections bound to the dead loop ->
``RuntimeError: ... attached to a different loop`` /
``InterfaceError: another operation is in progress``.

The reconciler previously raised on
every tick after the first. ``kb.index_file`` did NOT, because it routes
all DB work through ``run_in_short_session`` (per-call NullPool engine,
disposed inside the same ``asyncio.run`` — nothing survives loop
teardown).

These tests drive the REAL sync DBOS entry points twice in a row —
exactly what beat does over two ticks in one process — against the real
pytest-postgresql DB. They must NOT inject a fixture-owned engine into
``db._admin_engine`` (that engine is bound to the test's own loop and
would mask the bug); instead they point ``ADMIN_DATABASE_URL`` at the
superuser test DB and reset the cached singleton, so each tick goes
through the exact production acquisition path. On current (broken) code
the SECOND call raises; after the per-call-disposed-engine fix both
ticks succeed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import psycopg
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from vibecanvas_api.storage.repo_tasks import TasksRepo


def _seed_stuck_task(pg_url: str) -> uuid.UUID:
    """Insert one encrypted stuck task through a disposable async engine.

    Tenant/user FK rows use psycopg. The task itself must use ``TasksRepo`` so
    the strict ciphertext-only schema is exercised. Its one-off NullPool
    engine is disposed before ``asyncio.run`` closes, so no connection can be
    inherited by either reconciler tick being tested.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    sync_dsn = pg_url.replace("+asyncpg", "")
    with psycopg.connect(sync_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants(tenant_id, name) VALUES (%s, 'x')",
            (tenant_id,),
        )
        conn.execute(
            "INSERT INTO users(user_id, tenant_id, email) VALUES (%s, %s, %s)",
            (user_id, tenant_id, f"beat-{uuid.uuid4().hex[:6]}@example.com"),
        )

    async def _seed() -> None:
        engine = create_async_engine(pg_url, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                task = await TasksRepo(session).create(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workflow_id=None,
                    task_type="batch_exec",
                    payload={},
                    background_job_id=str(task_id),
                )
                task.submitted_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=120)
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    return task_id


def _point_admin_engine_at_test_db(monkeypatch, pg_url: str) -> None:
    """Force every ``get_admin_engine`` call through the production path.

    Reset the cached singleton to ``None`` and set ``ADMIN_DATABASE_URL``
    to the superuser test DB, so the engine is (re)built from the env var
    exactly as in production — NOT swapped for a fixture engine bound to
    the test loop.
    """
    from vibecanvas_api.storage import db as db_mod

    monkeypatch.setattr(db_mod, "_admin_engine", None)
    # asyncpg URL form for create_async_engine.
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)


def test_reconciler_survives_two_beat_ticks(monkeypatch, pg_url):
    """Two sequential beat ticks of the reconciler must both succeed.

    Calls the SYNC DBOS entry point ``resubmit_stuck_queued`` twice —
    each does its own ``asyncio.run(_resubmit())`` (= two beat ticks in
    one long-lived process). On broken code the second call raises a
    loop-bound ``RuntimeError`` / ``InterfaceError``; after the fix both
    return cleanly and re-publish the stuck row on each tick.
    """
    import vibecanvas_api.background_tasks.reconciler as recon

    _point_admin_engine_at_test_db(monkeypatch, pg_url)
    _seed_stuck_task(pg_url)

    sent: list[dict] = []
    monkeypatch.setattr(
        recon,
        "enqueue_background_job",
        lambda name, **kw: sent.append({"name": name, **kw}),
    )

    # Tick #1 — binds + (on broken code) leaves a loop-bound global pool.
    recon.resubmit_stuck_queued()
    # Tick #2 — broken code: RuntimeError attached to a different loop.
    recon.resubmit_stuck_queued()

    # The stuck row is still queued (reconciler never UPDATEs), so it is
    # re-published on BOTH ticks — proving the second tick reached the DB.
    assert len(sent) == 2, f"expected one resubmit per tick, got {sent}"
    assert all(s["name"] == "batch_exec" for s in sent)


def test_kb_gc_sweeper_survives_two_beat_ticks(monkeypatch, pg_url):
    """Two sequential beat ticks of the KB GC sweeper must both succeed.

    ``kb_gc_sweeper`` opens an admin connection (phase-1 SELECT) AND an
    admin transaction (phase-3 DELETE) per tick — both through the
    process-global pool on broken code. With no doomed KBs seeded the
    body is a no-op, isolating the engine-lifecycle bug on tick #2.
    """
    import vibecanvas_api.background_tasks.kb_gc_sweeper as gc

    _point_admin_engine_at_test_db(monkeypatch, pg_url)

    gc.kb_gc_sweeper()
    gc.kb_gc_sweeper()


def test_kb_orphan_reconciler_survives_two_beat_ticks(monkeypatch, pg_url):
    """Two sequential beat ticks of the KB orphan reconciler must succeed.

    ``kb_orphan_reconciler`` opens an admin transaction (Case A UPDATE +
    Case B SELECT) per tick through the process-global pool on broken
    code. With no orphan rows seeded the per-row Case B write loop is
    skipped, isolating the admin-engine lifecycle on tick #2.
    """
    import vibecanvas_api.background_tasks.kb_orphan_reconciler as orphan

    _point_admin_engine_at_test_db(monkeypatch, pg_url)

    orphan.kb_orphan_reconciler()
    orphan.kb_orphan_reconciler()
