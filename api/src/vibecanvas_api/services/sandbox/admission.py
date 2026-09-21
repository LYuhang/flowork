"""Host-level concurrency admission for sandbox instances (P1).

The ONLY concurrent-execution cap today is ``WarmPoolManager.max_workers``,
which the warm-pool retirement (P7) removes. This module is its replacement at
the Sandbox layer: a process-wide ``asyncio.Semaphore`` bounding how many
sandbox instances may run at once, with a blocking acquire (callers queue) or a
non-blocking acquire that raises :class:`SandboxCapacityExceeded` (the 429 path).

The cap is (re)configured from ``config.sandbox_max_concurrent`` via
:func:`configure_admission`; call it once at app startup. The default cap is
applied lazily on first use so imports don't depend on a configured app.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager

from vibecanvas_api.services.sandbox.redis_admission import (
    RedisAdmission,
    SandboxCapacityExceeded as SandboxCapacityExceeded,  # re-export (single type)
)

_DEFAULT_CAP = 8
_sem: "asyncio.Semaphore | None" = None
_cap: int = _DEFAULT_CAP

# --- cross-process (Redis-backed) sync admission ---------------------------
# Lazily built RedisAdmission + a degraded threading.Semaphore fallback so a
# missing/unreachable Redis NEVER crashes a run (it just loses cross-process
# bounding for that process).
_redis_adm: "RedisAdmission | None" = None
_redis_tried: bool = False
_fallback_sem: "threading.Semaphore | None" = None
_sync_lk = threading.Lock()


def configure_admission(cap: int) -> None:
    """Set the concurrent-sandbox cap (call once at startup from config). Resets
    the semaphore — do not call while runs are in flight."""
    global _sem, _cap, _redis_adm, _redis_tried, _fallback_sem
    _cap = max(1, int(cap))
    _sem = asyncio.Semaphore(_cap)
    # Reset the sync side so it rebuilds against the new cap.
    _redis_adm = None
    _redis_tried = False
    _fallback_sem = None


def _get_sem() -> "asyncio.Semaphore":
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(_cap)
    return _sem


def _inflight() -> int:
    """Number of slots currently held (for tests / metrics)."""
    sem = _get_sem()
    return _cap - sem._value  # type: ignore[attr-defined]


@asynccontextmanager
async def sandbox_admission(*, block: bool = True):
    """Acquire one sandbox slot for the duration of the ``async with`` body.

    ``block=True`` (default) → await a free slot (callers queue under load).
    ``block=False`` → raise :class:`SandboxCapacityExceeded` immediately if the
    cap is reached (the route maps it to a 429 / "capacity exceeded" message).
    The slot is ALWAYS released on exit, including on exception."""
    sem = _get_sem()
    if block:
        await sem.acquire()
    else:
        if sem.locked() or sem._value == 0:  # type: ignore[attr-defined]
            raise SandboxCapacityExceeded(f"sandbox cap {_cap} reached")
        await sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _get_redis_adm() -> "RedisAdmission | None":
    """Lazily build the Redis-backed sync admission. Returns ``None`` (and arms
    the threading-semaphore fallback) if Redis can't be reached — FAIL-SOFT, so
    a missing daemon never crashes a run; it only loses cross-process bounding
    for this process."""
    global _redis_adm, _redis_tried, _fallback_sem
    with _sync_lk:
        if _redis_adm is not None:
            return _redis_adm
        if _redis_tried:
            return None
        _redis_tried = True
        try:
            import redis  # local import keeps module import cheap / optional

            from vibecanvas_api.config import config

            client = redis.from_url(
                config.redis.url,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            # Touch the server so an unreachable Redis fails HERE, not mid-run.
            client.ping()
            _redis_adm = RedisAdmission(client, cap=_cap)
            return _redis_adm
        except Exception:
            # Degraded: process-local cap. Better than 2×cap or a crash.
            _fallback_sem = threading.Semaphore(_cap)
            return None


@contextmanager
def sync_sandbox_admission(*, tenant_id: str, block: bool = True):
    """Synchronous, cross-process sandbox admission for the background worker batch path.

    Each batch row runs its own ``asyncio.run`` on a ThreadPoolExecutor thread,
    so the async :func:`sandbox_admission` (bound to one event loop) cannot bound
    it. This goes through a shared Redis counter (see
    :class:`RedisAdmission`) so the SAME cap holds across the async server loop
    AND every batch thread. If Redis is unavailable it FAIL-SOFTs to a
    process-local ``threading.Semaphore`` (degraded, never crashes).

    ``block`` is honored on BOTH paths: the Redis counter poll-queues until a
    slot frees (v1 wait) and the fallback semaphore blocks; ``block=False``
    raises :class:`SandboxCapacityExceeded` immediately (the 429 path).
    """
    adm = _get_redis_adm()
    if adm is not None:
        with adm.slot(tenant_id=tenant_id, block=block):
            yield
        return
    # --- degraded process-local fallback ---
    sem = _fallback_sem
    if sem is None:  # pragma: no cover - defensive; _get_redis_adm arms it
        sem = threading.Semaphore(_cap)
    if block:
        sem.acquire()
    else:
        if not sem.acquire(blocking=False):
            raise SandboxCapacityExceeded(f"sandbox cap {_cap} reached")
    try:
        yield
    finally:
        sem.release()
