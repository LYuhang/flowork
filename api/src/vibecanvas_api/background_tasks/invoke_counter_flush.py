"""Flush Redis invoke counters into ``deployments.invoke_count``.

Scheduled every 60 seconds by DBOS. If Redis is empty or unavailable, this is
a best-effort no-op.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from vibecanvas_api.services.rate_limit import _get_redis
from vibecanvas_api.services.tenant_db import session_scope_admin


FLUSH_INTERVAL_SEC = 60.0


def flush_invoke_counters():
    asyncio.run(_flush())


async def _flush() -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        keys = await r.keys("dep:*:count")
    except Exception:
        return
    updates: list[tuple[str, int]] = []
    for k in keys:
        key_str = k.decode() if isinstance(k, bytes) else k
        try:
            dep_id = key_str.split(":")[1]
        except (IndexError, AttributeError):
            continue
        try:
            n = await r.getdel(key_str)
        except Exception:
            continue
        if n is not None:
            try:
                n_int = int(n.decode() if isinstance(n, bytes) else n)
            except (ValueError, AttributeError):
                continue
            if n_int > 0:
                updates.append((dep_id, n_int))
    if not updates:
        return
    async with session_scope_admin() as s:
        for dep_id, delta in updates:
            await s.execute(text(
                "UPDATE deployments SET invoke_count = invoke_count + :n, "
                "last_invoked_at = now() WHERE id = :id"
            ), {"id": dep_id, "n": delta})
