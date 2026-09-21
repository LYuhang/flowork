"""Async engine + session factory. One engine per process; one session
per request via `get_db` FastAPI dependency. SSE handlers do NOT hold a
session for the stream lifetime — they open short sessions per write
and tenant-bound sessions."""
from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from vibecanvas_api.config import config

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_admin_engine: AsyncEngine | None = None

# Guard ``init_engine`` check-then-create so concurrent startup
# concurrent first-call race (lifespan startup vs. an early request /
# the SyncRefRepo warm path) cannot build two engines + lose a pool.
# Guards an in-process one-time singleton init — NOT a DI-serialization
# lock (rule #5 only forbids using a lock to serialize DI sessions).
_ENGINE_LOCK = threading.Lock()


def init_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    # Double-checked locking: the fast path skips the lock once built;
    # the slow path takes _ENGINE_LOCK and re-checks so exactly one
    # engine + sessionmaker is ever created (carried T3 race fix).
    if _engine is None:
        with _ENGINE_LOCK:
            if _engine is None:
                _engine = create_async_engine(
                    config.database.url,
                    pool_size=config.database.pool_size,
                    max_overflow=config.database.max_overflow,
                    pool_pre_ping=True,
                    pool_recycle=config.database.pool_recycle,
                    # PgBouncer transaction-mode safety.
                    connect_args={"prepared_statement_cache_size": 0},
                )
                _sessionmaker = async_sessionmaker(
                    _engine, expire_on_commit=False
                )
    return _engine


def get_engine() -> AsyncEngine:
    return init_engine()


async def dispose_engine(*, close: bool = True) -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose(close=close)
        _engine = None
        _sessionmaker = None


@asynccontextmanager
async def session_scope(
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """Short-lived session for SSE / background writes. Commits on
    success, rolls back on exception, always closes.

    When ``tenant_id`` or ``user_id`` is given, sets the transaction-local
    RLS context GUCs. ``app.user_id`` admits only caller-owned recipient
    projection rows; it is not an authorization decision. This is REQUIRED for
    the SSE per-message / per-node short-session writes: they run in
    their own transactions, and `set_config(..., is_local=true)` is
    transaction-scoped, so without this they would hit FORCE RLS with no
    tenant context and have every insert silently rejected by RLS.
    `set_config` (not `SET LOCAL`) is used because it accepts a bound
    parameter — no string interpolation, no injection risk."""
    init_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as s:
        try:
            if tenant_id is not None:
                await s.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": tenant_id})
            if user_id is not None:
                await s.execute(
                    text("SELECT set_config('app.user_id', :u, true)"),
                    {"u": user_id},
                )
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def short_session_scope(
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """Drop-in for :func:`session_scope` for the run/session-lifecycle
    write-back + release sites — same commit/rollback/RLS semantics, but
    backed by a PER-CALL ``NullPool`` engine instead of the process-global
    pooled singleton.

    WHY (cross-loop write-back data-loss): the global ``_engine`` binds its
    asyncpg connections to whichever event loop first built it (a request
    loop, an early ``asyncio.run`` from a sync repo facade / background worker tick /
    the frozen-agent bridge). The VFS write-back / release functions can run
    on a DIFFERENT loop than the one the singleton was bound to. Reusing a
    connection across loops raises ``RuntimeError: ... attached to a different
    loop`` / ``Event loop is closed`` / ``InterfaceError: another operation
    is in progress`` — which the fail-soft write-back swallows, so the files
    the agent/run wrote into the VFS run dirs are silently LOST. VFS is
    foundational infra, so this must not happen.

    The remedy mirrors :func:`storage.sync_session.run_in_short_session`
    (sync) and :func:`short_admin_connection` (admin): build a fresh engine
    on the CURRENT running loop, use it, and ``await engine.dispose()`` in
    ``finally`` *inside the same frame*, so NO connection ever survives across
    loops. These call sites are already async / on a running loop, so there is
    NO ``asyncio.run`` and NO thread bridge here — the whole point is just to
    avoid the cross-loop GLOBAL pool. The global engine + ``session_scope`` +
    ``get_db`` are deliberately left untouched; the request/DI path keeps the
    pooled engine.

    ``NullPool`` opens+closes a real connection per use (fine at per-run /
    per-session-retirement frequency) and ``prepared_statement_cache_size=0``
    is pgbouncer transaction-mode safety, mirroring ``init_engine``. When
    ``tenant_id`` is given, the transaction-local ``app.tenant_id`` GUC is set
    via a bound ``set_config`` BEFORE yielding (RLS), exactly as
    ``session_scope`` does.
    """
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
                        text("SELECT set_config('app.tenant_id', :t, true)"),
                        {"t": tenant_id})
                if user_id is not None:
                    await s.execute(
                        text("SELECT set_config('app.user_id', :u, true)"),
                        {"u": user_id},
                    )
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise
    finally:
        await engine.dispose()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — one session per request."""
    async with session_scope() as s:
        yield s


def get_admin_engine() -> AsyncEngine:
    """Singleton async engine bound to the admin (RLS-bypassing) DB URL.

    This is only for system-owned cross-tenant sweeps.
    Never call from a user-request route handler.

    Production uses ``MAINTENANCE_DATABASE_URL``: a non-superuser role that
    may bypass RLS but cannot own objects or perform DDL. Development/test
    retain the legacy alias and fallback so existing fixtures can inject the
    superuser engine without weakening the production startup gate.
    """
    global _admin_engine
    if _admin_engine is None:
        _admin_engine = create_async_engine(
            maintenance_database_url(), future=True
        )
    return _admin_engine


def maintenance_database_url() -> str:
    """Resolve the cross-tenant control-plane connection.

    ``ADMIN_DATABASE_URL`` remains a development/test compatibility alias.
    Production validation rejects it and requires the explicitly named,
    non-superuser ``MAINTENANCE_DATABASE_URL``.
    """
    return (
        os.environ.get("MAINTENANCE_DATABASE_URL")
        or os.environ.get("ADMIN_DATABASE_URL")
        or config.database.url
    )
