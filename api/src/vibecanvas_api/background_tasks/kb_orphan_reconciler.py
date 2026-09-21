"""KB/RAG T6 — sweep ``kb_files`` rows orphaned by upload-path failure.

The upload route (POST /kb/:id/files) is 5-step + DB-first to prevent
S3 orphans (spec sec 8 ``Upload route ordering``):

  Step 2: INSERT kb_files (status='pending', object_store_key=NULL)
  Step 3: write blob to object_store
  Step 4: UPDATE kb_files.object_store_key
  Step 5: enqueue durable indexing work

A failure at step 3 leaves a ``kb_files`` row with
``object_store_key IS NULL`` and ``status='pending'`` — Case A here.
A failure at step 5 leaves a ``kb_files`` row WITH ``object_store_key`` set,
but no worker processing it — Case B here.

This DBOS-scheduled task runs every 5 minutes:

Both cases become an explicit failed state with a user-facing diagnostic. The
platform never silently retries work: a user or Agent starts a new operation
with a new task identity after inspecting the failure.

Uses the admin engine (RLS-bypassing) because this is a system-owned
cross-tenant sweep — see reconciler.py docstring.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_kb import KbRepo
from vibecanvas_api.storage.sync_session import short_admin_connection


ORPHAN_THRESHOLD_SEC = 60
INTERVAL_SEC = 300  # 5 minutes — frequent enough that orphans surface


def kb_orphan_reconciler():
    """Background entry point — run the async sweep on a fresh event loop."""
    asyncio.run(_sweep())


async def _sweep() -> None:
    """Sweep orphan ``kb_files`` rows (Case A + Case B; see module docstring).

    Cross-tenant discovery uses the admin connection. Each mutation then runs
    in the row's tenant-bound transaction so RLS and encrypted private fields
    follow the same repository path as normal requests.
    """
    async with short_admin_connection() as conn:
        rows = (await conn.execute(text(f"""
            SELECT id::text AS file_id,
                   tenant_id::text AS tenant_id,
                   (object_store_key IS NOT NULL) AS object_written
              FROM kb_files kf
             WHERE kf.status='pending'
               AND kf.deleted_at IS NULL
               AND kf.created_at < now() - interval '{ORPHAN_THRESHOLD_SEC} seconds'
        """))).mappings().all()

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ORPHAN_THRESHOLD_SEC)
    for row in rows:
        if row["object_written"]:
            message = (
                "Indexing did not start after upload completed. "
                "Start indexing again to create a new task."
            )
        else:
            message = (
                "Upload failed because the object-store write did not complete. "
                "Upload the file again to create a new task."
            )
        async with session_scope(tenant_id=row["tenant_id"]) as session:
            await KbRepo(session).fail_pending_if_stale(
                uuid.UUID(row["file_id"]),
                older_than=cutoff,
                error_message=message,
            )
