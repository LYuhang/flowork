"""Spec §7.2 — daily reconciliation of Redis concurrency counters from Postgres truth."""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from vibecanvas_api.services.rate_limit import _get_redis
from vibecanvas_api.services.tenant_db import session_scope_admin


RECONCILE_INTERVAL_SEC = 86400.0


def reconcile_concurrency():
    asyncio.run(_reconcile())


async def _reconcile() -> None:
    r = _get_redis()
    if r is None:
        return
    async with session_scope_admin() as s:
        rows = (await s.execute(text(
            "SELECT t.tenant_id, "
            "       COALESCE(COUNT(tk.id) FILTER (WHERE tk.status IN ('queued','running')), 0) AS n "
            "FROM tenants t LEFT JOIN tasks tk ON tk.tenant_id = t.tenant_id "
            "GROUP BY t.tenant_id"
        ))).all()
    for row in rows:
        try:
            await r.set(f"cc:tenant:{row.tenant_id}", row.n)
        except Exception:
            continue
