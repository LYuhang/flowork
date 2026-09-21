"""Async tenant ContextVar — bound by ``resolve_deployment_and_bind_tenant``
for external API and webhook Deployment flows.

The user-request path uses the explicit ``current_user`` + ``tenant_db`` DI
(JWT → request.state → session-scoped GUC); this is the deployment-side
parallel: the tenant is discovered via ``api_key_hash`` / ``slug``
admin-role lookup, then bound here so the rest of the call graph (which
uses ``session_scope(tenant_id=...)``) can read it via
``tenant_id_var.get()`` and forward it explicitly.

The sync facade has its own ContextVar (``storage/sync_session.py:
current_sync_tenant_id``) read inside ``run_in_short_session``. THIS CV
is the async parallel: it is READ by external-flow route handlers /
service methods which then PASS the value explicitly to
``session_scope(tenant_id=...)``. ``session_scope`` itself takes a
keyword argument, NOT the CV — that keeps the helper composable for
the user-JWT path that already has a tenant_id from auth.

Every external deployment endpoint must call
``resolve_deployment_and_bind_tenant`` BEFORE any tenant-bound DB op.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Optional
from uuid import UUID

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from vibecanvas_api.storage import db as _db


tenant_id_var: ContextVar[Optional[UUID]] = ContextVar(
    "tenant_id_var", default=None,
)


@asynccontextmanager
async def session_scope_admin() -> AsyncIterator[AsyncSession]:
    """Admin-role async session — RLS-bypassing. ONLY for system code that
    needs to look up rows before the tenant context is known (deployment
    slug / api_key resolution and bounded platform maintenance).

    Mirrors ``db.py:session_scope`` (commit-on-success, rollback-on-
    exception) but binds to the restricted maintenance connection
    (``MAINTENANCE_DATABASE_URL``; legacy aliases are non-production only).
    The caller decides the
    transaction shape — this just owns the session lifecycle.

    Never call from a user-request route handler — its session must be
    tenant-scoped so RLS applies.

    FIX-beat (loop-bound engine): the background worker-BEAT periodic tasks
    (invoke-counter flush and concurrency reconciler) call
    this inside ``asyncio.run`` once per tick, in a single long-lived
    worker process. The old implementation bound the *process-global*
    pooled ``get_admin_engine()`` to tick #1's loop, which ``asyncio.run``
    then closed — so every later tick crashed with
    ``RuntimeError: ... attached to a different loop``. We now build a
    per-call ``NullPool`` engine and dispose it in ``finally`` inside the
    same ``asyncio.run`` (mirrors ``storage.sync_session`` short-session),
    so nothing survives loop teardown. The test fixture injection via
    ``db._admin_engine`` is honoured (reused, not disposed).
    """
    injected = _db._admin_engine
    engine = injected if injected is not None else create_async_engine(
        _db.maintenance_database_url(),
        poolclass=NullPool,
        connect_args={"prepared_statement_cache_size": 0},
    )
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
