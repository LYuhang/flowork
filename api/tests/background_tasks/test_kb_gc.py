"""KB/RAG T11 — 30-day GC sweeper test.

Plan §11.3 specifies a single test that covers the
:func:`vibecanvas_api.background_tasks.kb_gc_sweeper._sweep` contract:

1. Create a KB, soft-delete it (Tier 1 — DB row stays around).
2. Backdate ``deleted_at`` to 31 days ago via raw SQL.
3. Run ``_sweep()`` and assert the row is physically gone (Tier 2).

The plan draft used ``admin_session`` / ``tenant_session`` /
``tenant_id`` / ``user_id`` fixtures, but this repo's conftest does not
ship them. We follow the same pattern as ``test_repo_kb.py`` (seed
tenants + users through the RLS-bypassing ``pg_engine``; drive
``KbRepo`` via ``session_scope(tenant_id=...)``) and
``test_batch_submit_and_reconciler.py`` (monkeypatch
``db_mod._admin_engine`` to point ``get_admin_engine`` at ``pg_engine``
so the sweeper sees the test DB).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_gc_deletes_after_30d(monkeypatch, pg_engine):
    """A KB soft-deleted >30 days ago must be physically deleted."""
    from vibecanvas_api.background_tasks.kb_gc_sweeper import _sweep
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.repo_kb import KbRepo
    from vibecanvas_api.storage.db import session_scope

    # Point the admin engine at pg_engine so the sweeper hits the test DB.
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    # Seed tenant + user (auth tables are RLS-free; superuser engine OK).
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'gc')"),
            {"t": tenant_id},
        )
        await c.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:u, :t, :e)"
            ),
            {"u": user_id, "t": tenant_id,
             "e": f"gc-{uuid.uuid4().hex[:6]}@example.com"},
        )

    # Create + soft-delete the KB via the tenant-scoped session — exactly
    # the path a user-action ``DELETE /kb/:id`` would take. We split
    # create and soft-delete into separate ``session_scope`` blocks
    # because ``set_config(..., is_local=true)`` is transaction-scoped:
    # after the first commit the GUC is cleared and the second UPDATE
    # would hit FORCE RLS with no tenant context.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        kb = await KbRepo(s).create_kb(
            tenant_id=tenant_id, user_id=user_id, name="ToBeGCed",
        )
    kb_id = kb.id

    async with session_scope(tenant_id=str(tenant_id)) as s:
        await KbRepo(s).soft_delete_kb(kb_id)

    # Backdate ``deleted_at`` past the 30-day retention window so the
    # sweeper's WHERE clause picks it up. Direct raw SQL through the
    # superuser engine — there's no Repo method for this (and there
    # shouldn't be; this is a test-only time-travel hack).
    async with pg_engine.begin() as c:
        await c.execute(
            text("UPDATE knowledge_bases SET deleted_at = "
                 "now() - interval '31 days' WHERE id = :id"),
            {"id": kb_id},
        )

    await _sweep()

    # Row must be physically gone — CASCADE cleaned up kb_files /
    # kb_chunks transitively, which we verify by counting remaining
    # KB rows by id (the rest is the CASCADE contract guaranteed by
    # the alembic migration + Postgres itself).
    async with pg_engine.connect() as c:
        result = await c.execute(
            text("SELECT count(*) FROM knowledge_bases WHERE id = :id"),
            {"id": kb_id},
        )
        assert result.scalar() == 0
