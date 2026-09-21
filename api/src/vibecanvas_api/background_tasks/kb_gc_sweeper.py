"""KB/RAG T11 — 30-day GC sweeper (Tier-2 hard delete).

Spec §4.6 defines a two-tier delete model:

* Tier 1 — user clicks Delete → ``UPDATE knowledge_bases SET deleted_at
  = now()`` (and cascade-UPDATE ``kb_files``). Rows + S3 blobs + chunks
  remain physically present but are invisible to every read path.
* Tier 2 — this sweeper, every 6 hours, issues a real ``DELETE FROM
  knowledge_bases WHERE deleted_at < now() - 30 days``. The ``ON
  DELETE CASCADE`` constraints on ``kb_files`` / ``kb_chunks`` finally
  fire (they never fired on the soft-delete UPDATE), so all three
  tables get physically purged in one transaction.

Why we collect S3 prefixes BEFORE the DB DELETE
-----------------------------------------------
Once the ``knowledge_bases`` row is gone the CASCADE has already
removed every ``kb_files`` row under it, taking the
``(tenant_id, kb_id)`` pair that determines the S3 prefix with them.
We therefore SELECT the doomed rows first, derive ``kb/{tenant}/{kb}/``
prefixes, ask the object store to bulk-delete by prefix, and ONLY
THEN issue the DB DELETE. S3 failures are non-fatal (the orphaned
blobs are unreachable and the next sweeper run will not retry — the
DB row is gone — but that is acceptable for V1; an out-of-band
audit job could find them via prefix listing if ever needed).

Why ``get_admin_engine()``
--------------------------
This is a system-owned cross-tenant sweep — it must see deleted rows
under every tenant in one query. ``FORCE RLS`` would gate the read by
``current_setting('app.tenant_id')`` and return nothing. The admin
engine bypasses RLS (same pattern as ``reconciler.py`` +
``kb_orphan_reconciler.py``).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.sync_session import short_admin_connection


GC_INTERVAL_SEC = 6 * 60 * 60  # every 6 hours
RETENTION_DAYS = 30


def kb_gc_sweeper():
    """Background entry point — run the async sweep on a fresh event loop.

    Same shape as ``kb_orphan_reconciler``: sync wrapper around an
    async ``_sweep`` so the asyncpg-backed admin engine is happy.
    """
    asyncio.run(_sweep())


async def _sweep() -> None:
    """Sweep KBs whose ``deleted_at`` is older than ``RETENTION_DAYS``.

    Two-phase to keep S3 cleanup decoupled from the DB DELETE:

    1. SELECT doomed rows + compute S3 prefixes.
    2. Best-effort ``delete_prefix`` per row (continue on error).
    3. Single DELETE that fires CASCADE on ``kb_files`` / ``kb_chunks``.
    """
    # First collect tenant and knowledge-base identifiers before deleting,
    # after CASCADE we can no longer recover them.
    async with short_admin_connection() as conn:
        result = await conn.execute(text(f"""
            SELECT id::text AS kb_id, tenant_id::text AS tenant_id
              FROM knowledge_bases
             WHERE deleted_at IS NOT NULL
               AND deleted_at < now() - interval '{RETENTION_DAYS} days'
        """))
        rows = result.mappings().all()

    # Then perform a best-effort object-store prefix delete. Swallow exceptions
    # so a single bad blob can't stop the sweeper from cleaning the DB
    # — a row left around forever is a worse failure mode than a
    # handful of orphan blobs (which are unreachable anyway since the
    # presigned-URL path lives behind ``kb_files``).
    store = get_object_store()
    for row in rows:
        prefix = f"kb/{row['tenant_id']}/{row['kb_id']}/"
        try:
            store.delete_prefix(prefix)
        except Exception:
            # Best-effort: log-only in V2; silent in V1.
            pass

    # Finally delete database rows; cascades remove files and chunks.
    async with short_admin_connection() as conn, conn.begin():
        await conn.execute(text(f"""
            DELETE FROM knowledge_bases
             WHERE deleted_at IS NOT NULL
               AND deleted_at < now() - interval '{RETENTION_DAYS} days'
        """))
