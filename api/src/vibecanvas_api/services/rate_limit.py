"""Redis fixed-window Deployment QPS enforcement and best-effort counters.

Configured QPS requires Redis availability; unavailable enforcement returns 503.
Usage counters remain best-effort and do not change invocation admission.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import HTTPException

from vibecanvas_api.config import config


_redis_client: Any = None
_redis_init_failed: bool = False


def _get_redis():
    """Lazy Redis client. Returns None if Redis is configured-but-down.

    Admission rejects unavailable enforcement; best-effort counters skip it.
    """
    global _redis_client, _redis_init_failed
    if _redis_client is not None:
        return _redis_client
    if _redis_init_failed:
        return None
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(
            config.redis.url,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        return _redis_client
    except Exception:
        _redis_init_failed = True
        return None


_FIXED_WINDOW_LUA = """
local key = KEYS[1]
local n = redis.call('INCR', key)
if n == 1 then redis.call('EXPIRE', key, 1) end
return n
"""


async def check_rate_limit(deployment: dict) -> None:
    """429 on QPS overflow; 503 when a configured limit cannot be enforced."""
    qps = deployment.get("rate_limit_qps")
    if qps is None or qps == 0:
        return
    r = _get_redis()
    if r is None:
        raise HTTPException(503, detail="rate_limit_unavailable", headers={"Retry-After": "1"})
    key = f"rl:dep:{deployment['id']}"
    try:
        count = int(await r.eval(_FIXED_WINDOW_LUA, 1, key))
    except Exception as exc:
        raise HTTPException(503, detail="rate_limit_unavailable", headers={"Retry-After": "1"}) from exc
    if int(count) > qps:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": "1"},
        )


async def bump_redis_invoke_counter(deployment_id: Any) -> None:
    """Best-effort counter bump in Redis. invoke_counter_flush rolls these
    into deployments.invoke_count on a beat schedule (§4.4)."""
    r = _get_redis()
    if r is None:
        return
    try:
        await r.incr(f"dep:{deployment_id}:count")
        await r.set(f"dep:{deployment_id}:last_invoked_at", str(time.time()))
    except Exception:
        return


async def check_tenant_concurrency(tenant_id: Any, *, increment: bool) -> None:
    """Atomic incr-with-cap on a per-tenant Redis counter.

    increment=False is a decrement (call on task completion).
    Cap comes from tenants.max_concurrent_deployments — None means unlimited.
    """
    r = _get_redis()
    if r is None:
        return
    key = f"cc:tenant:{tenant_id}"
    if not increment:
        try:
            await r.decr(key)
        except Exception:
            pass
        return
    cap = await _get_tenant_cap(tenant_id)
    if cap is None:
        return
    try:
        n = await r.incr(key)
    except Exception:
        return
    if int(n) > cap:
        # Roll back our bump; raise 429.
        try:
            await r.decr(key)
        except Exception:
            pass
        raise HTTPException(429, detail="tenant concurrency cap reached")


async def _get_tenant_cap(tenant_id: Any) -> Optional[int]:
    """Read max_concurrent_deployments via the admin engine; cache 60s in Redis."""
    r = _get_redis()
    if r is None:
        return None
    cache_key = f"tenant:{tenant_id}:cap"
    try:
        cached = await r.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        try:
            text_val = cached.decode() if isinstance(cached, bytes) else cached
        except Exception:
            text_val = ""
        if text_val == "NONE":
            return None
        try:
            return int(text_val)
        except ValueError:
            return None
    # Fresh read.
    from sqlalchemy import text as sql_text
    from vibecanvas_api.services.tenant_db import session_scope_admin
    async with session_scope_admin() as s:
        row = (await s.execute(sql_text(
            "SELECT max_concurrent_deployments FROM tenants WHERE tenant_id = :t"
        ), {"t": tenant_id})).one_or_none()
    val = row.max_concurrent_deployments if row else None
    try:
        await r.set(cache_key, str(val) if val is not None else "NONE", ex=60)
    except Exception:
        pass
    return val
