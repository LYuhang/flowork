# -*- coding: utf-8 -*-
"""Generic, engine-agnostic bounded subprocess pool (extracted from code_runner).

A LEAN, bounded, lazily-grown pool of worker subprocesses that the parent
spawns / routes jobs to / times out / kills. Isolation is NOT this layer's job
(the selected OS sandbox provides the isolation boundary) — it only manages worker
lifecycle and the job/result transport.

Two consumers reuse this skeleton (A1, sandbox parallel-execution design):
  * CodeNode — ``CodeWorkerPool`` (worker script: ``code_worker.py``, runs ``process_fn``).
  * Sandbox parallel serve — a pool over ``job_worker.py`` (runs one workflow job).

Design (carried verbatim from the original CodeWorkerPool):
  * Per-pool, bounded (``max_workers``), lazily grown. ``run()`` is thread-safe:
    each worker is checked out by exactly one ``run()`` at a time (no interleaving
    on a single worker's control channel); concurrent callers get distinct idle
    workers or spawn up to the cap. The slow ``subprocess.Popen`` happens OUTSIDE
    the lock (slot reserved first) so parallel spawns stay concurrent.
  * Hard per-call timeout → SIGKILL just that worker + respawn; other workers
    untouched. Worker dying without a result (pipe EOF) → crash → respawn.
  * The control channel is a pair of inherited OS pipes per worker (NOT
    stdin/stdout), so user ``print``/``stdin`` can't corrupt the job/result
    protocol. Framing = 4-byte big-endian length + UTF-8 JSON body.
  * Pure stdlib — keep it that way (it is imported inside the sandbox).
"""

import json
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time

_LEN = struct.Struct(">I")


class JobKilled(Exception):
    """The supervisor received a kill request for one job, not the pool."""


def _remaining(deadline, kill_path=None):
    if kill_path and os.path.exists(kill_path):
        raise JobKilled()
    remaining = deadline - time.monotonic() if deadline is not None else None
    if remaining is not None and remaining <= 0:
        raise TimeoutError("worker call timed out")
    return remaining


class _Worker:
    """One spawned worker subprocess + its two control pipes.

    Pipes (parent's view):
      * ``job_w``    — parent writes framed jobs here; child reads ``job_r``.
      * ``result_r`` — parent reads framed results here; child writes ``result_w``.

    ``no_site`` adds ``-S`` (skip the interpreter's default site-packages) so a
    stdlib-only worker imports 3rd-party libs ONLY from its ``PYTHONPATH`` (the
    CodeNode lib-overlay contract). A worker that must import the engine/api
    (e.g. ``job_worker``) passes ``no_site=False``.
    """

    def __init__(self, worker_script, cwd, env, no_site=True, own_process_group=False):
        self._own_process_group = own_process_group
        # job pipe: parent writes job_w, child reads job_r
        job_r, job_w = os.pipe()
        # result pipe: child writes result_w, parent reads result_r
        try:
            result_r, result_w = os.pipe()
        except BaseException:
            os.close(job_r)
            os.close(job_w)
            raise
        self._job_w = job_w
        self._result_r = result_r
        argv = [sys.executable]
        if no_site:
            argv.append("-S")
        argv += [worker_script, str(job_r), str(result_w)]
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                pass_fds=(job_r, result_w),
                start_new_session=own_process_group,
                # Leave stdout/stderr to inherit (control is on the dedicated
                # pipes); close_fds defaults True so no OTHER parent fds leak.
            )
        except BaseException:
            os.close(job_w)
            os.close(result_r)
            raise
        finally:
            # The child holds its ends now; the parent must close them so EOF
            # propagates correctly when either side dies.
            os.close(job_r)
            os.close(result_w)

    def send_job(self, job: dict, deadline, *, kill_path=None):
        """Send input within the call deadline, even if the child stops reading."""
        body = json.dumps(job).encode("utf-8")
        frame = _LEN.pack(len(body)) + body
        view = memoryview(frame)
        total = 0
        os.set_blocking(self._job_w, False)
        while total < len(frame):
            remaining = _remaining(deadline, kill_path)
            wait = min(remaining, 0.1) if remaining is not None else 0.1
            _, writable, _ = select.select([], [self._job_w], [], wait if kill_path else remaining)
            if not writable:
                continue
            try:
                total += os.write(self._job_w, view[total:])
            except BlockingIOError:
                continue

    def read_result(self, deadline, *, kill_path=None):
        """Read one result within the same monotonic call deadline."""
        header = self._read_exact(_LEN.size, deadline, kill_path)
        (length,) = _LEN.unpack(header)
        body = self._read_exact(length, deadline, kill_path)
        return json.loads(body.decode("utf-8"))

    def _read_exact(self, n, _deadline, kill_path=None):
        buf = bytearray()
        while len(buf) < n:
            remaining = _remaining(_deadline, kill_path)
            wait = min(remaining, 0.1) if remaining is not None else 0.1
            r, _, _ = select.select([self._result_r], [], [], wait if kill_path else remaining)
            if not r:
                continue
            chunk = os.read(self._result_r, n - len(buf))
            if not chunk:
                raise EOFError("worker closed pipe before result")
            buf.extend(chunk)
        return bytes(buf)

    def kill(self):
        """SIGKILL the worker and close the parent's pipe ends. Idempotent."""
        stopped = self.kill_proc_only()
        for fd in (self._job_w, self._result_r):
            try:
                os.close(fd)
            except Exception:
                pass
        return stopped

    def kill_proc_only(self):
        """SIGKILL the worker subprocess but DO NOT close the parent's pipe fds.

        Used when ANOTHER thread (the pool ``close()`` during cancellation) must
        kill a worker that its owning ``run()`` is still blocked reading from:
        SIGKILL makes the child's pipe ends close → the owner's blocked
        ``read_result`` sees EOF and unwinds via its own ``kill()`` (which then
        closes the fds). Closing the fds HERE would race that concurrent
        ``os.read`` on the same descriptor. Idempotent."""
        try:
            if self._own_process_group:
                os.killpg(self.proc.pid, signal.SIGKILL)
            else:
                self.proc.send_signal(signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            return False
        try:
            self.proc.wait(timeout=5)
        except Exception:
            return False
        return True

    def terminate(self):
        """Graceful-ish shutdown: close the job pipe (worker sees EOF → exits),
        then ensure it's gone."""
        if self._own_process_group:
            return self.kill()
        try:
            os.close(self._job_w)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.send_signal(signal.SIGKILL)
                self.proc.wait(timeout=5)
            except Exception:
                pass
        try:
            os.close(self._result_r)
        except Exception:
            pass
        return self.proc.poll() is not None


class BoundedSubprocessPool:
    """A bounded, thread-safe pool of worker subprocesses over a pluggable script."""

    def __init__(self, worker_script, cwd, env, max_workers=4, no_site=True, own_process_group=False):
        self._worker_script = worker_script
        self._cwd = cwd
        self._env = env
        self._no_site = no_site
        self._own_process_group = own_process_group
        self._max_workers = max(1, int(max_workers))

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._idle = []          # list[_Worker] available for checkout
        self._busy = set()       # set[_Worker] checked out + mid-run (close() can kill in-flight)
        self._total = 0          # spawned-and-alive count (idle + busy + reserved-but-not-yet-spawned)
        self._closed = False

    def _spawn(self):
        """Spawn a fresh worker subprocess. The caller has already RESERVED the
        slot (bumped ``_total`` under the lock); this runs OUTSIDE the lock so
        concurrent acquisitions don't serialize on the (slow) ``Popen`` fork/exec."""
        return _Worker(self._worker_script, self._cwd, self._env, self._no_site,
                       own_process_group=self._own_process_group)

    def _acquire(self, kill_path=None, *, deadline=None):
        """Check out a worker: reuse an idle one, else spawn up to the cap, else
        wait for one to be returned/freed. Recorded in ``_busy`` so ``close()``
        can SIGKILL it even while mid-run (cancellation)."""
        with self._cond:
            while True:
                remaining = _remaining(deadline, kill_path)
                if self._closed:
                    raise RuntimeError("BoundedSubprocessPool is closed")
                if self._idle:
                    w = self._idle.pop()
                    self._busy.add(w)
                    return w
                if self._total < self._max_workers:
                    self._total += 1   # reserve under the lock; spawn outside it
                    break
                wait = min(remaining, 0.1) if remaining is not None else 0.1
                self._cond.wait(timeout=wait if kill_path else remaining)
        # --- outside the lock: the slow part ---
        try:
            w = self._spawn()
        except BaseException:
            with self._cond:
                self._total -= 1       # give the reserved slot back
                self._cond.notify()
            raise
        with self._cond:
            if self._closed:
                self._total -= 1
                w.kill()
                self._cond.notify_all()
                raise RuntimeError("BoundedSubprocessPool is closed")
            self._busy.add(w)
        return w

    def _release(self, worker):
        with self._cond:
            self._busy.discard(worker)
            if self._closed:
                worker.terminate()
                self._cond.notify_all()
                return
            self._idle.append(worker)
            self._cond.notify()

    def _discard(self, worker):
        """A worker is dead (killed/crashed); drop it from the live count so a
        future ``_acquire`` can spawn a replacement, and wake a waiter."""
        with self._cond:
            self._busy.discard(worker)
            self._total -= 1
            self._cond.notify()

    def run(self, job: dict, timeout, *, timeout_msg=None, crash_msg=None, kill_path=None) -> dict:
        """Run one ``job`` (a JSON-serializable dict) on a worker with a hard
        ``timeout`` covering acquisition, startup, input and result transport.
        Process creation and JSON serialization are synchronous; their elapsed
        time consumes this budget, checked before dispatch. Returns the result
        on success or an error envelope on timeout / worker crash. ``timeout_msg`` / ``crash_msg``
        customize the error text (consumers keep their own wording)."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        if kill_path and os.path.exists(kill_path):
            return {"status": "cancelled", "error_message": "Job was stopped before execution."}
        try:
            worker = self._acquire(kill_path=kill_path, deadline=deadline)
        except JobKilled:
            return {"status": "cancelled", "error_message": "Job was stopped while waiting for a worker."}
        except TimeoutError:
            return {"status": "error", "error_message": timeout_msg or f"job timed out after {timeout:g}s"}
        try:
            _remaining(deadline, kill_path)  # Include process startup time.
            worker.send_job(job, deadline, kill_path=kill_path)
            result = worker.read_result(deadline, kill_path=kill_path)
        except JobKilled:
            stopped = worker.kill()
            self._discard(worker)
            if not stopped:
                return {"ok": False, "status": "unknown", "error_message": "Worker kill could not be confirmed."}
            return {"status": "cancelled", "error_message": "Job worker was killed."}
        except TimeoutError:
            worker.kill()
            self._discard(worker)
            return {"status": "error",
                    "error_message": timeout_msg or f"job timed out after {timeout:g}s"}
        except (EOFError, BrokenPipeError, OSError):
            worker.kill()
            self._discard(worker)
            return {"status": "error", "error_message": crash_msg or "worker crashed"}
        else:
            self._release(worker)
            return result

    def close(self):
        """Terminate ALL workers — idle, in-flight (busy), and any returned later.
        Idempotent + fail-soft. IDLE workers get a graceful ``terminate()``; BUSY
        workers get a hard ``kill_proc_only()`` so the owning ``run()`` thread,
        blocked in ``read_result``, sees the pipe EOF and closes the fds itself."""
        with self._cond:
            already = self._closed
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            busy = list(self._busy)
            reserved = self._total - len(idle) - len(busy)
            # Do NOT clear _busy — the owning run() discards its worker itself.
            self._cond.notify_all()
        if already:
            return getattr(self, "_close_confirmed", False)
        confirmed = reserved == 0
        for w in busy:
            try:
                confirmed = (w.kill_proc_only() is not False) and confirmed
            except Exception:
                confirmed = False
        for w in idle:
            try:
                confirmed = (w.terminate() is not False) and confirmed
            except Exception:
                confirmed = False
        self._close_confirmed = confirmed
        return confirmed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
