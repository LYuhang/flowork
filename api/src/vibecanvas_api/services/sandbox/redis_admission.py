"""Redis-backed sandbox concurrency admission (P1 cutover).

Why Redis and not :mod:`asyncio.Semaphore`? Workflow execution runs in TWO
worlds at once:

* the async FastAPI server loop (one event loop, many coroutines), and
* the background worker batch ThreadPoolExecutor, where **each batch row does its own**
  ``asyncio.run`` — a fresh event loop per thread.

An ``asyncio.Semaphore`` is bound to ONE event loop, so it cannot bound across
the batch threads/loops; a process-local ``threading.Semaphore`` would be a
SECOND, independent counter that desyncs from the async one (→ effectively
``2 × cap`` sandboxes). A Redis ``INCR``/``DECR`` counter is
process/thread/loop-agnostic — and is the first brick of a future per-tenant
resource governor (hence the ``tenant_id`` argument, recorded for that future
even though the v1 cap is global).

The counter carries a TTL (``EXPIRE`` on every successful acquire) so a crashed
holder's increment self-frees instead of leaking a slot forever.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

# Default poll interval for the block=True wait loop (seconds). This is a v1
# poll-based wait (a slot frees → next poll picks it up); real fair queueing is
# the per-tenant resource governor follow-up.
_DEFAULT_POLL_INTERVAL_S = 0.05

# Per-process peak/in-flight tracking — for TESTS and metrics only. The
# authoritative cross-process count lives in Redis; these locals just let a
# single-process test assert "never more than cap held concurrently".
_peak = 0
_inflight = 0
_lk = threading.Lock()


class SandboxCapacityExceeded(Exception):
    """No sandbox slot was free and the caller asked NOT to block."""


class RedisAdmission:
    """Bound concurrent sandbox instances via a shared Redis counter.

    One instance wraps one Redis client + one counter key. Use :meth:`slot`
    as a context manager around the lifetime of a sandbox instance.
    """

    def __init__(self, redis, *, cap: int, key: str = "sbx:concurrent",
                 ttl_s: int = 600,
                 poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S):
        self._r = redis
        self._cap = max(1, int(cap))
        self._key = key
        self._ttl = ttl_s
        self._poll = poll_interval_s

    @contextmanager
    def slot(self, *, tenant_id: str, block: bool = True):
        """Hold one sandbox slot for the duration of the ``with`` body.

        ``INCR`` the shared counter; if it overshoots ``cap`` → ``DECR`` back.
        With ``block=True`` (batch / deployment), QUEUE: poll-retry on a short
        interval until a slot frees, then yield — matching the threading
        ``Semaphore`` fail-soft path (which blocks), so a row over cap waits
        rather than being permanently failed / 500'd. With ``block=False``,
        raise :class:`SandboxCapacityExceeded` immediately (the 429 path). On a
        successful acquire the key's TTL is refreshed (crash-safety). The slot
        is ALWAYS released (``DECR``) on exit, including on exception.

        v1 poll-based wait: the per-key TTL (``ttl_s``) guarantees a crashed
        holder's slot self-frees, so the loop can't wedge forever. Real fair
        queueing is the per-tenant resource governor follow-up — ``tenant_id``
        is recorded for it even though the v1 cap is global.
        """
        global _peak, _inflight
        while True:
            n = self._r.incr(self._key)
            if n <= self._cap:
                break
            self._r.decr(self._key)
            if not block:
                raise SandboxCapacityExceeded(
                    f"sandbox cap {self._cap} reached")
            # Queue: a slot will free when a holder exits (DECR) or its TTL
            # expires (crash-safety). Poll again after a short interval.
            time.sleep(self._poll)
        # Safety TTL: a crashed holder's INCR self-expires instead of leaking.
        try:
            self._r.expire(self._key, self._ttl)
        except Exception:
            pass
        with _lk:
            _inflight += 1
            _peak = max(_peak, _inflight)
        try:
            yield
        finally:
            with _lk:
                _inflight -= 1
            self._r.decr(self._key)


def _peak_for_test() -> int:
    return _peak


def _peak_reset_for_test() -> None:
    global _peak, _inflight
    _peak = 0
    _inflight = 0
