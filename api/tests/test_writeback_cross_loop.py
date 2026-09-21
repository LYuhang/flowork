# -*- coding: utf-8 -*-
"""Regression: VFS run/session write-back must not silently lose files when it
runs on a DIFFERENT event loop than the one the process-global pooled engine was
first bound to.

THE BUG (root cause): ``storage/db.py``'s global ``_engine`` (built by
``init_engine`` and used by ``session_scope``) binds its asyncpg connections to
whatever event loop FIRST created it. The codebase has several places that drive
their own loop (sync repo facades / DBOS ticks / the frozen-agent bridge), so a
run/session-lifecycle write-back can execute on a loop OTHER than the one the
singleton is bound to. Reusing a connection across loops raises
``RuntimeError: ... attached to a different loop`` / ``Event loop is closed`` /
``InterfaceError: another operation is in progress`` — which the fail-soft
write-back swallows, so the files the agent/run wrote into the VFS run dirs are
LOST. The fix: ``short_session_scope`` builds a PER-CALL ``NullPool`` engine on
the CURRENT loop, disposed in ``finally`` inside the same frame, so no connection
ever crosses loops.

STRATEGY USED: the REAL two-loop persistence test (preferred over the monkeypatch
fallback). We force two genuinely distinct loops with the real test Postgres:
  * Loop A — a standalone ``asyncio.run`` opens the GLOBAL pooled ``session_scope``
    and runs ``SELECT 1``, binding ``db._engine`` to Loop A; that loop then CLOSES.
  * Loop B — a SEPARATE ``asyncio.run`` calls the converted ``sync_run_back``,
    which writes a real file from a temp run dir into ``vfs_run``.
We assert (a) Loop B does NOT raise a cross-loop / closed-loop error, and (b) the
file is actually persisted (read the bytes back through the run-tier repo on a
THIRD fresh loop and assert they match).

REPRODUCTION CONFIRMED: pointing ``vfs_run_context.short_session_scope`` back at
the global ``session_scope`` makes Loop B reuse the Loop-A-bound pool and the
write-back raises the cross-loop error (file lost). See
``test_repro_old_session_scope_loses_file_across_loops``, which patches in the
old behaviour and asserts the failure mode, locking the regression in place.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import vibecanvas_api.services.vfs_run_context as rc_mod
from vibecanvas_api.config import config
from vibecanvas_api.services.object_store import InMemoryObjectStore
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo


# A loop-mismatch failure surfaces as one of these asyncpg/SQLAlchemy errors.
_LOOP_ERR_MARKERS = (
    "attached to a different loop",
    "event loop is closed",
    "another operation is in progress",
    "got Future",
)


def _is_loop_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _LOOP_ERR_MARKERS)


async def _commit_tenant() -> str:
    """Commit a real ``tenants`` row (vfs_run FKs to it) via a per-call engine so
    a SEPARATE connection/loop can see it. Returns the tenant hex."""
    tenant = uuid.uuid4()
    engine = create_async_engine(
        config.database.url, poolclass=NullPool,
        connect_args={"prepared_statement_cache_size": 0})
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                {"t": tenant})
    finally:
        await engine.dispose()
    return tenant.hex


async def _read_run_file(tenant: str, store, run_id: str, path: str) -> bytes:
    """Read a /run file back through the run-tier repo on a fresh per-call engine
    (a THIRD loop), proving it was actually persisted (row + blob)."""
    engine = create_async_engine(
        config.database.url, poolclass=NullPool,
        connect_args={"prepared_statement_cache_size": 0})
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            await s.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": tenant})
            repo = VfsRunRepo(s, store, tenant)
            return await repo.read_bytes(run_id=run_id, path=path)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_short_session_scope_writeback_survives_cross_loop(
        monkeypatch, tmp_path):
    """REAL two-loop persistence test (see module docstring). With the converted
    ``sync_run_back`` (per-call ``short_session_scope``) the run file written on a
    loop OTHER than the one the global pool is bound to is persisted, no error."""
    tenant = await _commit_tenant()

    store = InMemoryObjectStore()
    monkeypatch.setattr(rc_mod, "get_object_store", lambda: store)

    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    payload = b"data written by a node on the wrong loop"
    with open(os.path.join(run_dir, "out.txt"), "wb") as f:
        f.write(payload)

    run_id = "cross-loop-" + uuid.uuid4().hex[:8]

    # Loop A — prime + bind the GLOBAL pooled engine, then close this loop.
    def _prime_global() -> None:
        async def _body() -> None:
            async with session_scope() as s:
                await s.execute(text("SELECT 1"))
        asyncio.run(_body())

    # Loop B — a SEPARATE asyncio.run drives the converted write-back. It must NOT
    # touch the Loop-A-bound global pool (it builds its own per-call engine).
    def _writeback() -> int:
        return asyncio.run(rc_mod.sync_run_back(run_id, tenant, run_dir))

    # Run both on a worker thread so each asyncio.run owns a brand-new loop that
    # is fully torn down before the next — the exact cross-loop condition. (The
    # autouse _isolate_global_engine fixture disposes the global only at test
    # setup/teardown, NOT between these inner asyncio.run calls.)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(_prime_global).result()
        try:
            synced = ex.submit(_writeback).result()
        except Exception as exc:  # pragma: no cover - only on regression
            if _is_loop_error(exc):
                pytest.fail(f"cross-loop write-back regressed: {exc!r}")
            raise

    assert synced == 1  # the file was synced (not silently swallowed)

    # And it is REALLY persisted: read the bytes back on a third fresh loop.
    got = await _read_run_file(tenant, store, run_id, "/run/out.txt")
    assert got == payload


@pytest.mark.asyncio
async def test_repro_old_session_scope_loses_file_across_loops(
        monkeypatch, tmp_path):
    """Confirms the regression genuinely reproduces with the OLD behaviour:
    point ``sync_run_back``'s scope back at the GLOBAL ``session_scope`` and the
    cross-loop write-back raises a loop-mismatch error (→ file lost via fail-soft
    in production). This locks in that the converted-code test above is real."""
    tenant = await _commit_tenant()

    store = InMemoryObjectStore()
    monkeypatch.setattr(rc_mod, "get_object_store", lambda: store)
    # Revert ONLY this site to the global pooled engine (the pre-fix code).
    monkeypatch.setattr(rc_mod, "short_session_scope", session_scope)

    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "out.txt"), "wb") as f:
        f.write(b"lost?")

    run_id = "repro-" + uuid.uuid4().hex[:8]

    def _prime_global() -> None:
        async def _body() -> None:
            async with session_scope() as s:
                await s.execute(text("SELECT 1"))
        asyncio.run(_body())

    def _writeback():
        return asyncio.run(rc_mod.sync_run_back(run_id, tenant, run_dir))

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(_prime_global).result()
        # The OLD code reuses the Loop-A-bound global pool on Loop B → either the
        # write raises a loop error, or fail-soft swallows it and 0 files persist.
        # Both are the data-loss bug; assert we see one of them.
        try:
            synced = ex.submit(_writeback).result()
        except Exception as exc:
            assert _is_loop_error(exc), f"expected a loop error, got {exc!r}"
            return

    # No raise → the per-file fail-soft swallowed the loop error: nothing synced.
    assert synced == 0, (
        "expected the old global-pool path to lose the file across loops, "
        f"but it synced {synced}")
