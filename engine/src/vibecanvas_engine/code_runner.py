# -*- coding: utf-8 -*-
"""Parent-side per-RUN CodeNode worker pool.

A LEAN, per-run, bounded pool of ``code_worker.py`` subprocesses. Isolation is
the selected OS sandbox's job (the workflow runs inside it), so this layer does NO
jailing — it only spawns/routes/timeouts/kills. The generic pool/worker/pipe
machinery lives in :mod:`vibecanvas_engine.subprocess_pool`
(``BoundedSubprocessPool``); ``CodeWorkerPool`` is the CodeNode-specific subclass
that pins the worker script, the allowlist env, and the ``run(code, inputs, …)``
shape. (A1: the skeleton was extracted so the sandbox parallel-execution serve
loop can reuse the SAME pool over a different worker script — ``job_worker.py``.)

Key properties (implemented in the base pool):
  * Per-RUN, bounded (``max_workers``), lazily grown. ``run()`` is thread-safe —
    CodeNode calls arrive on ``asyncio.to_thread`` threads, possibly CONCURRENT
    for parallel workflow branches; each worker is checked out by exactly one
    ``run()`` at a time, concurrent callers get distinct idle workers or spawn up
    to the cap, and ``Popen`` happens OUTSIDE the lock so parallel spawns don't
    serialize.
  * Hard per-call timeout → SIGKILL just that worker + respawn; others untouched.
  * Control channel = a pair of inherited OS pipes per worker (NOT stdin/stdout),
    so user ``print``/``stdin`` can't corrupt the protocol. Framing = 4-byte
    big-endian length + JSON.

CodeNode-specific here:
  * EXPLICIT-ALLOWLIST env (C1): only ``PATH`` / ``PYTHONPATH`` /
    ``VC_CODE_PYTHONPATH`` / ``HOME`` / ``LC_ALL`` / ``LANG`` + the
    egress-proxy passthrough (``HTTP_PROXY`` /
    ``HTTPS_PROXY`` / ``NO_PROXY`` — the CONTROLLED, allowlisted egress path in
    prod ``--network=none``, NOT a secret). NEVER ``os.environ`` wholesale — host
    secrets (``DATABASE_URL`` / ``OPENAI_API_KEY``) never leak in.
  * ``-S`` (no implicit site): interpreter startup receives an EMPTY
    ``PYTHONPATH``. After the stdlib shim has booted it appends
    ``VC_CODE_PYTHONPATH`` (Workflow overlay first, explicitly mounted platform
    packages second) to ``sys.path``. This keeps stdlib authoritative while
    allowing a Workflow pin to override a base third-party package.
"""

import os

from vibecanvas_engine.subprocess_pool import BoundedSubprocessPool, _Worker  # noqa: F401 (re-export _Worker for callers/tests)

# Absolute path to the worker shim. We launch it as a STANDALONE SCRIPT
# (``python <path> <fds>``), NOT as ``python -m vibecanvas_engine.code_worker``:
# ``-m`` forces ``vibecanvas_engine/__init__`` to import the WHOLE engine
# (workflow → nodes → asyncio/sandbox → ~2.3s cold, and ~4.5s when two workers
# spawn concurrently for parallel branches — which serialized parallel CodeNodes
# and defeated the per-run pool). The shim is deliberately stdlib-only and has an
# ``if __name__ == "__main__"`` entry, so running the file directly skips the
# package import entirely → ~0.1s startup, and parallel spawns stay concurrent.
_WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "code_worker.py")


class CodeWorkerPool(BoundedSubprocessPool):
    """A per-RUN, bounded, thread-safe pool of CodeNode worker subprocesses.

    Thin subclass of :class:`BoundedSubprocessPool`: pins the CodeNode worker
    script + the allowlist env, and adapts ``run`` to the ``(code, inputs)``
    job shape with CodeNode's error wording. The pool/worker/timeout machinery
    is inherited (so e.g. tests that subclass-and-override ``_spawn`` still work).
    """

    # Egress-proxy env that MUST reach the worker so user HTTP libs (requests/
    # httpx) route through the in-sandbox forward proxy → host broker allowlist.
    # These are the egress CONTROL plane, not secrets. Absent (dev/host-network
    # mode) → not set, so the worker behaves identically.
    _EGRESS_PASSTHROUGH = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")

    def __init__(self, pythonpath, cwd, max_workers=4):
        self._pythonpath = pythonpath
        super().__init__(
            worker_script=_WORKER_SCRIPT,
            cwd=cwd,
            env=self._build_env(pythonpath),
            max_workers=max_workers,
            no_site=True,
        )

    @classmethod
    def _build_env(cls, pythonpath):
        """EXPLICIT-ALLOWLIST env (C1/M3). NEVER inherit os.environ wholesale —
        no DATABASE_URL / OPENAI_API_KEY / other host secrets leak in. Only the
        few vars a normal Python subprocess needs, PLUS the egress-proxy vars
        (the controlled egress path; NOT secrets)."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            # Never expose the overlay during Python startup: PYTHONPATH entries
            # precede the stdlib and can therefore shadow modules such as enum.
            # The stdlib-only worker appends this explicit path after boot.
            "PYTHONPATH": "",
            "VC_CODE_PYTHONPATH": pythonpath,
            "HOME": os.environ.get("HOME", "/root"),
        }
        # Keep encoding sane for json/text I/O without leaking anything.
        env["LC_ALL"] = os.environ.get("LC_ALL", "C.UTF-8")
        env["LANG"] = os.environ.get("LANG", "C.UTF-8")
        # Forward ONLY the egress-proxy control vars (present iff the provider
        # launched the engine in proxy mode).
        for k in cls._EGRESS_PASSTHROUGH:
            v = os.environ.get(k)
            if v is not None:
                env[k] = v
        return env

    def run(self, code, inputs, timeout):
        """Run ``process_fn`` from ``code`` on ``inputs`` with a hard ``timeout``.

        Returns a result envelope dict:
          * success → ``{"status": "success", "output": {...}}``
          * user error / non-JSON / non-dict → ``{"status": "error", "error_message", "traceback"}``
          * timeout → ``{"status": "error", "error_message": "code node timed out after <N>s"}``
          * worker crash → ``{"status": "error", "error_message": "code worker crashed"}``
        """
        return super().run(
            {"code": code, "inputs": inputs},
            timeout,
            timeout_msg=f"code node timed out after {timeout:g}s",
            crash_msg="code worker crashed",
        )
