"""Shared per-call short-session driver for the sync repo/manager facades.

The exact per-call
short-session boilerplate was copy-pasted verbatim in
``SyncWorkflowRepo._run`` (``storage/sync_repo.py``) and
``SyncRefRepo._run`` (``storage/ref_repo.py``) — and is now also needed
by synchronous repository facades. This is the block where the
T6 loop-bound-engine fix lives, so duplicating it means a future fix
must be applied N times. Extracted here once.

The frozen agent (``agent.py``) and its vibe/canvas tools are frozen
sync code that call repos/managers synchronously from a daemon worker
thread / fork subprocess (no running event loop, no request session).
A request-scoped async session must not be shared with that thread, and
SSE handlers must not hold a session for the stream lifetime. So each
call opens its OWN short-lived engine:

* ``NullPool`` — a real connection is opened+closed per use
  (acceptable at per-agent-turn / per-ref / per-template frequency), so
  NO pooled connection ever survives this ``asyncio.run``. The next
  call's fresh event loop therefore never inherits a connection bound
  to this (now-closed) loop — the T6 ``RuntimeError: Event loop is
  closed`` / "attached to a different loop" crash. The engine is
  disposed in ``finally`` *inside the same* ``asyncio.run``.
* ``connect_args={"prepared_statement_cache_size": 0}`` — pgbouncer
  transaction-mode safety, matching ``db.py:init_engine``.
* ``async_sessionmaker(expire_on_commit=False)`` — repo methods read
  ORM attributes after the commit.

Semantics are byte-identical to the previous two ``_run``
implementations: same engine kwargs, same commit-on-success /
rollback-on-exception / dispose-in-finally ordering, all inside one
``asyncio.run``. ``db.py``'s global ``init_engine``/``session_scope``
are left untouched — they correctly serve the async DI request path.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine,
)

from vibecanvas_api.config import config
from vibecanvas_api.storage import db as _db

T = TypeVar("T")

# Agent and reference-resolution writes to RLS-protected
# tables through the sync facades, but ``run_in_short_session`` opens its
# own transaction OUTSIDE any request's ``tenant_db`` scope, so without a
# tenant context the 7 business tables' ``FORCE ROW LEVEL SECURITY`` +
# ``tenant_id DEFAULT current_setting('app.tenant_id', true)`` would
# leave ``tenant_id`` NULL → NOT-NULL violation, and reads would see only
# public rows. Threading a ``tenant_id`` parameter through ~10 tool call
# sites + the process-global singletons would be a huge, fragile change,
# so the current tenant is carried in this ``ContextVar`` instead.
#
# It is set at the top of ``agent.run_agent_turn``
# (the async entry point — the legacy daemon-thread bridge that used to
# set it is gone) and in the refs route, and READ here. ContextVars
# propagate into ``asyncio.run`` (the coroutine captures the calling
# thread's context at creation) and are copied by ``asyncio.to_thread``,
# so a value set at the async entry point is visible to every
# ``run_in_short_session`` call the agent turn / ref-resolve makes
# (whether it stays on the loop or hops to a worker thread).
current_sync_tenant_id: ContextVar[str | None] = ContextVar(
    "vibecanvas_sync_tenant_id", default=None)


def run_in_short_session(
    coro_factory: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run ``coro_factory(session)`` in a fresh short-lived session.

    ``coro_factory`` receives the live :class:`AsyncSession` and returns
    a coroutine; its result is returned after the session commits. On
    any exception the session is rolled back and the exception
    re-raised. The per-call NullPool engine is always disposed inside
    this ``asyncio.run`` to avoid loop-bound shared state.

    If ``current_sync_tenant_id`` is set, the session runs
    ``SELECT set_config('app.tenant_id', :t, true)`` BEFORE
    ``coro_factory`` so Postgres RLS isolates this short-session write to
    the right tenant — exactly mirroring ``db.py:session_scope``. The CV
    is read here in the calling thread/context (``_runner``'s coroutine
    captures it at creation), so the agent-thread / ref-resolve boundary
    that sets it is honoured.
    """

    tenant_id = current_sync_tenant_id.get()

    async def _runner() -> Any:
        engine = create_async_engine(
            config.database.url,
            poolclass=NullPool,
            connect_args={"prepared_statement_cache_size": 0},
        )
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                try:
                    if tenant_id is not None:
                        await s.execute(
                            text("SELECT set_config('app.tenant_id', "
                                 ":t, true)"),
                            {"t": tenant_id})
                    result = await coro_factory(s)
                    await s.commit()
                    return result
                except Exception:
                    await s.rollback()
                    raise
        finally:
            await engine.dispose()

    # Loop-safe bridge. With NO running loop (the common case — a tool dispatched
    # via asyncio.to_thread, a background worker task) ``asyncio.run`` is correct. But when
    # called from WITHIN a running event loop (e.g. the compaction/edit middleware
    # runs inside the agent's async turn), ``asyncio.run`` raises "cannot be called
    # from a running event loop" — which silently no-ops every middleware-side VFS
    # access (S2a/S2b caches, head+tail re-hydration, state.md re-injection). In
    # that case run ``_runner`` on a dedicated worker thread with its own loop.
    # ``tenant_id`` was captured above (before the thread), so RLS still binds.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_runner())  # no running loop — direct
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
        return _ex.submit(lambda: asyncio.run(_runner())).result()


# ---------------------------------------------------------------------------
# Admin short-session / short-connection (FIX-beat — loop-bound engine)
# ---------------------------------------------------------------------------
#
# The background worker-BEAT periodic tasks (reconciler, kb gc / orphan sweepers,
# invoke-counter flush, concurrency reconciler) run in a
# single long-lived prefork worker process and each does
# ``asyncio.run(_async_body())`` per tick. They used the *process-global*
# pooled admin engine (``db.get_admin_engine`` / ``session_scope_admin``),
# whose asyncpg pool gets bound to tick #1's event loop — which
# ``asyncio.run`` then CLOSES. Every later tick reuses the same cached
# engine against a now-dead loop -> ``RuntimeError: ... attached to a
# different loop`` / ``InterfaceError: another operation is in progress``.
#
# These two helpers mirror :func:`run_in_short_session` exactly but bind
# to the ADMIN (RLS-bypassing) DB URL: a per-call ``NullPool`` engine,
# disposed in ``finally`` *inside the same* ``asyncio.run``, so nothing
# survives loop teardown and the next tick's fresh loop never inherits a
# dead-loop-bound connection.
#
# Test-injection compatibility: ``conftest`` swaps the singleton via
# ``monkeypatch.setattr(db, "_admin_engine", pg_engine)`` so the sweepers
# hit the test DB. When that override is present we REUSE it (and never
# dispose it — the fixture owns its lifecycle). Otherwise we build our
# own per-call engine from the restricted maintenance URL / ``config`` —
# identical resolution to ``db.get_admin_engine``.


def _admin_url() -> str:
    """Admin DB URL — same resolution as ``db.get_admin_engine``."""
    return _db.maintenance_database_url()


def _make_admin_engine():
    """Per-call NullPool admin engine (disposed by the caller's finally)."""
    return create_async_engine(
        _admin_url(),
        poolclass=NullPool,
        connect_args={"prepared_statement_cache_size": 0},
    )


@asynccontextmanager
async def short_admin_connection() -> AsyncIterator[AsyncConnection]:
    """Admin (RLS-bypassing) raw connection for cross-tenant sweeps.

    Drop-in for ``get_admin_engine().connect()`` /
    ``get_admin_engine().begin()`` callers that issue raw ``text()`` SQL
    (reconciler, kb gc / orphan sweepers). Yields a *connection* (NOT a
    transaction) — the caller decides ``conn.begin()`` vs autocommit-read,
    exactly as before. The per-call engine is disposed in ``finally``
    inside the running ``asyncio.run`` (loop-bound-state safety).

    If ``db._admin_engine`` was injected (test fixture), reuse it and do
    NOT dispose it — the fixture owns it.
    """
    injected = _db._admin_engine
    if injected is not None:
        async with injected.connect() as conn:
            yield conn
        return
    engine = _make_admin_engine()
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


@asynccontextmanager
async def short_admin_session() -> AsyncIterator[AsyncSession]:
    """Short-lived RLS-bypassing ORM session for encrypted control-plane scans.

    Cross-tenant workers that need to decrypt an application envelope must use
    an ``AsyncSession`` so the content-key ownership row is validated through
    the normal encryption service.  This keeps the same per-call ``NullPool``
    lifecycle as :func:`short_admin_connection` and never places KMS material
    in SQL or worker arguments.
    """
    injected = _db._admin_engine
    engine = injected or _make_admin_engine()
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        if injected is None:
            await engine.dispose()
