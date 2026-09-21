# -*- coding: utf-8 -*-
"""Credential-free API-side sandbox job dispatcher.

This entrypoint adds bounded file operations and sandbox-owned MCP connections
to the pure engine job protocol. It never imports the platform database layer,
never registers host/API data nodes, and never receives database, KMS, Redis,
Object Store or provider credentials. Platform data access is performed by
host brokers before/after the isolated job.

Run modes:
  * ``python -m vibecanvas_api.sandbox_entry <run_id> <tenant>`` — one-shot.
  * ``python -m vibecanvas_api.sandbox_entry serve <work_dir> <runs_root>`` —
    long-lived worker over the file-job channel. The tenant remains structural
    path metadata only and is never converted into database authority.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import tempfile
import threading
import time


def _append_extra_python_paths() -> None:
    for path in os.environ.get("VC_SANDBOX_PYTHON_PATHS", "").split(os.pathsep):
        if path and path not in sys.path:
            sys.path.append(path)


_append_extra_python_paths()

# The engine run-tier protocol is unchanged — we delegate to ``run_job`` and
# reuse its run-tier path helpers for the guard-result write. ``run_job`` is
# imported as a module-level name so tests can monkeypatch it.
from vibecanvas_engine.sandbox_entry import (  # noqa: E402
    run_job,
    _exec_dir as _engine_exec_dir,
    _write_result as _engine_write_result,
)


def _jsonable(value):
    """Best-effort conversion for MCP SDK return objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return _jsonable(value.dict())
        except Exception:
            pass
    return str(value)


def _exception_message(exc: BaseException) -> str:
    """Flatten async exception groups into an actionable MCP error."""
    if isinstance(exc, BaseExceptionGroup):  # noqa: F821
        messages = [_exception_message(child) for child in exc.exceptions]
        return "; ".join(message for message in messages if message)
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


async def _execute_mcp_operation(session, op: dict, server: dict, prefix: str) -> dict:
    """Execute one operation on an already-connected MCP session."""
    from datetime import timedelta

    action = op.get("action")
    if action == "manifest":
        tools_result = await session.list_tools()
        tools = []
        for tool in getattr(tools_result, "tools", []) or []:
            raw_name = getattr(tool, "name", "")
            tools.append({
                "name": f"{prefix}__{raw_name}",
                "raw_name": raw_name,
                "description": getattr(tool, "description", "") or "",
                "input_schema": _jsonable(
                    getattr(tool, "inputSchema", None)
                    or getattr(tool, "input_schema", None)
                    or {"type": "object", "properties": {}}
                ),
            })
        return {
            "ok": True,
            "status": "running",
            "server": server.get("name") or prefix,
            "prefix": prefix,
            "tools": tools,
        }
    if action == "call":
        raw_name = str(op.get("tool_name") or "")
        marker = f"{prefix}__"
        if raw_name.startswith(marker):
            raw_name = raw_name[len(marker):]
        result = await session.call_tool(
            raw_name,
            op.get("arguments") or {},
            read_timeout_seconds=timedelta(
                seconds=float(op.get("timeout_s") or 120.0)
            ),
        )
        return {
            "ok": not bool(getattr(result, "isError", False)),
            "result": _jsonable(result),
        }
    return {"ok": False, "error": f"unknown MCP action: {action!r}"}


class _McpActor:
    """One MCP connection owned by one long-lived event-loop task."""

    def __init__(self, runtime: "_SandboxMcpRuntime", key: str,
                 fingerprint: str, server: dict) -> None:
        self.runtime = runtime
        self.key = key
        self.fingerprint = fingerprint
        self.server = server
        self.queue: asyncio.Queue = asyncio.Queue()
        self.started = runtime.loop.create_future()
        self.task = runtime.loop.create_task(runtime._serve_actor(self))


class _SandboxMcpRuntime:
    """Persistent MCP sessions for the lifetime of one chat sandbox."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._actors: dict[str, _McpActor] = {}
        self._thread = threading.Thread(
            target=self._run_loop,
            name="vc-mcp-runtime",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    @staticmethod
    def _identity(server: dict) -> tuple[str, str]:
        key = str(server.get("id") or server.get("name") or server.get("tool_prefix"))
        fingerprint = json.dumps(
            server.get("connection") or {}, sort_keys=True, separators=(",", ":")
        )
        return key, fingerprint

    async def _serve_actor(self, actor: _McpActor) -> None:
        from vibecanvas_api.services.sandbox.mcp_client import mcp_client_session

        server = actor.server
        prefix = str(server.get("tool_prefix") or server.get("name") or "mcp")
        connection = server.get("connection") or {}
        try:
            if not connection:
                raise ValueError("missing MCP connection config")
            async with mcp_client_session(connection) as session:
                if not actor.started.done():
                    actor.started.set_result(None)
                while True:
                    item = await actor.queue.get()
                    if item is None:
                        break
                    op, answer = item
                    try:
                        result = await _execute_mcp_operation(
                            session, op, server, prefix
                        )
                    except Exception as exc:
                        result = {"ok": False, "error": _exception_message(exc)}
                        if not answer.done():
                            answer.set_result(result)
                        break
                    if not answer.done():
                        answer.set_result(result)
        except Exception as exc:
            error = _exception_message(exc)
            if not actor.started.done():
                actor.started.set_result(error)
        finally:
            while not actor.queue.empty():
                item = actor.queue.get_nowait()
                if item is not None:
                    _op, answer = item
                    if not answer.done():
                        answer.set_result({
                            "ok": False,
                            "error": "MCP connection closed; retry to reconnect",
                        })
            if self._actors.get(actor.key) is actor:
                self._actors.pop(actor.key, None)

    async def _dispatch(self, op: dict) -> dict:
        server = op.get("server") or {}
        key, fingerprint = self._identity(server)
        actor = self._actors.get(key)
        if actor is not None and (
            actor.fingerprint != fingerprint or actor.task.done()
        ):
            actor.queue.put_nowait(None)
            await actor.task
            actor = None
        if actor is None:
            actor = _McpActor(self, key, fingerprint, server)
            self._actors[key] = actor
        startup_error = await asyncio.shield(actor.started)
        if startup_error:
            return {"ok": False, "error": startup_error}
        answer = self.loop.create_future()
        actor.queue.put_nowait((op, answer))
        return await answer

    def run(self, op: dict) -> dict:
        timeout_s = float(op.get("timeout_s") or 120.0)
        coro = asyncio.wait_for(self._dispatch(op), timeout=timeout_s)
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout_s + 5.0)
        except Exception as exc:
            future.cancel()
            return {"ok": False, "error": _exception_message(exc)}

    async def _shutdown(self) -> None:
        actors = list(self._actors.values())
        for actor in actors:
            actor.queue.put_nowait(None)
        if actors:
            await asyncio.gather(
                *(actor.task for actor in actors), return_exceptions=True
            )
        self._actors.clear()

    def close(self) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            future.result(timeout=5.0)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5.0)


_mcp_runtime: _SandboxMcpRuntime | None = None
_mcp_runtime_lock = threading.Lock()


def _get_mcp_runtime() -> _SandboxMcpRuntime:
    global _mcp_runtime
    with _mcp_runtime_lock:
        if _mcp_runtime is None:
            _mcp_runtime = _SandboxMcpRuntime()
        return _mcp_runtime


def _close_mcp_runtime() -> None:
    global _mcp_runtime
    with _mcp_runtime_lock:
        runtime, _mcp_runtime = _mcp_runtime, None
    if runtime is not None:
        runtime.close()


def run_mcp_job(op: dict) -> dict:
    return _get_mcp_runtime().run(op)


class _MalformedRunSubpath(Exception):
    """A job's ``run_subpath`` escapes the runs root (absolute or contains ``..``).

    Raised inside the per-job ``try`` so it falls into the malformed-job handler:
    skip running, still write ``.done``, never crash (descriptor trust boundary).
    """


def run_one(run_root: str, run_id: str, tenant: str) -> int:
    """Delegate to the engine without deriving authority from ``tenant``.

    ``tenant`` is retained in the file-channel descriptor for path routing and
    audit correlation only. ``/run`` is execution-local scratch space; durable
    shared inputs belong in the user-level ``/mount`` namespace.
    """
    del tenant
    return run_job(run_root, run_id)


def serve_once_api(work_dir: str, runs_root: str) -> str | None:
    """Claim and serve one credential-free file-channel job.

    Job file layout (identical to the engine warm channel):
      ``{work_dir}/inbox/{job_id}.json``  — ``{"tenant", "run_id", ...}``
      ``{work_dir}/inbox/{job_id}.ready`` — atomic claim gate (host writes LAST)

    NEVER raises — the serve loop must survive any job.
    """
    try:
        inbox = os.path.join(work_dir, "inbox")
        ready = sorted(glob.glob(os.path.join(inbox, "*.ready")))
        if not ready:
            return None

        job_id = os.path.splitext(os.path.basename(ready[0]))[0]
        taken_path = os.path.join(inbox, f"{job_id}.taken")
        # The rename is the claim (atomic on POSIX); losing the race → no job.
        try:
            os.rename(os.path.join(inbox, f"{job_id}.ready"), taken_path)
        except OSError:
            return None

        try:
            with open(os.path.join(inbox, f"{job_id}.json"), "r",
                      encoding="utf-8") as f:
                job = json.load(f)
            if job.get("kind") == "fileop":
                # A fileop job carries NO tenant/run_id: run ONE file op inside
                # the sandbox (realpath-contained to runs_root) and return the
                # result via the outbox. ``run_fileop`` never raises.
                from vibecanvas_api.services.sandbox.fileops import run_fileop
                # Task 4b-i: confine file ops to the agent's CLEAN mount dests
                # (colon-separated absolute paths) when the worker sets
                # ``VIBECANVAS_FILEOP_ROOTS``; default ``[runs_root]`` keeps the
                # Task 2/3 single-/runs-root behavior unchanged.
                # Filter empty colon-segments (for example ``/data:``):
                # an empty root realpaths to the worker cwd /runs and would
                # silently re-admit it (defense-in-depth — warm.py only joins
                # non-empty dests today). An all-empty/garbage env falls back to
                # [runs_root]; never an empty roots list (which admits nothing).
                roots_env = os.environ.get("VIBECANVAS_FILEOP_ROOTS")
                roots = [r for r in roots_env.split(":") if r] if roots_env else [runs_root]
                if not roots:
                    roots = [runs_root]
                result = run_fileop(job.get("op") or {}, roots=roots)
                # Write the result JSON atomically BEFORE the .done marker, so
                # the host (which polls for .done, then reads result.json) never
                # observes .done without its result alongside it.
                outbox = os.path.join(work_dir, "outbox")
                os.makedirs(outbox, exist_ok=True)
                res_path = os.path.join(outbox, f"{job_id}.result.json")
                tmp_res = res_path + ".tmp"
                with open(tmp_res, "w", encoding="utf-8") as f:
                    json.dump(result, f)
                os.rename(tmp_res, res_path)
            elif job.get("kind") == "mcp":
                result = run_mcp_job(job.get("op") or {})
                outbox = os.path.join(work_dir, "outbox")
                os.makedirs(outbox, exist_ok=True)
                res_path = os.path.join(outbox, f"{job_id}.result.json")
                tmp_res = res_path + ".tmp"
                with open(tmp_res, "w", encoding="utf-8") as f:
                    json.dump(result, f)
                os.rename(tmp_res, res_path)
            elif job.get("kind") is not None:
                outbox = os.path.join(work_dir, "outbox")
                os.makedirs(outbox, exist_ok=True)
                res_path = os.path.join(outbox, f"{job_id}.result.json")
                tmp_res = res_path + ".tmp"
                with open(tmp_res, "w", encoding="utf-8") as f:
                    json.dump({
                        "ok": False,
                        "error": f"unknown_sandbox_job_kind: {job.get('kind')!r}",
                    }, f)
                os.rename(tmp_res, res_path)
            else:
                tenant = job["tenant"]
                run_id = job["run_id"]
                # Per-tenant warm pools mount ONLY {store_root}/run/{tenant} →
                # /runs, so the run lives at /runs/{run_id} (no tenant prefix);
                # the job then carries an explicit ``run_subpath``. Absent →
                # legacy {tenant}/{run_id}.
                sub = job.get("run_subpath")
                if sub is not None and (os.path.isabs(sub) or ".." in sub.split(os.sep)):
                    # Descriptor trust boundary (N3): an escaping subpath is
                    # MALFORMED — skip running it, still write .done below.
                    # NEVER raise out.
                    raise _MalformedRunSubpath(sub)
                run_root = (
                    os.path.join(runs_root, sub) if sub
                    else os.path.join(runs_root, tenant, run_id)
                )
                try:
                    run_one(run_root, run_id, tenant)
                except Exception as e:  # outer guard — never let the loop die.
                    try:
                        os.makedirs(_engine_exec_dir(run_root), exist_ok=True)
                        _engine_write_result(run_root, {}, {"__engine__": str(e)},
                                             0.0)
                    except Exception:
                        pass
        except Exception:
            # Malformed job json / missing tenant|run_id — still finish (.done
            # below) so the host never waits forever on it.
            pass

        # Write the .done marker LAST, atomically (temp then rename).
        outbox = os.path.join(work_dir, "outbox")
        os.makedirs(outbox, exist_ok=True)
        done_path = os.path.join(outbox, f"{job_id}.done")
        tmp_done = done_path + ".tmp"
        try:
            with open(tmp_done, "w", encoding="utf-8") as f:
                f.write("")
            os.rename(tmp_done, done_path)
        except Exception:
            pass

        # Drop the inbox markers (best-effort).
        for p in (taken_path, os.path.join(inbox, f"{job_id}.json")):
            try:
                os.remove(p)
            except OSError:
                pass

        return job_id
    except Exception:
        # Absolute backstop — serve_once_api NEVER raises.
        return None


def serve_loop_api(work_dir: str, runs_root: str,
                   poll_interval: float = 0.02) -> None:
    """Long-lived credential-free API job loop.

    Mirrors the engine ``serve_loop`` (orphan ``*.taken`` sweep on entry, poll
    ``inbox/*.ready`` via :func:`serve_once_api`, ``time.sleep`` when idle, exit
    on ``{work_dir}/shutdown``). No per-job database context exists here.
    """
    inbox = os.path.join(work_dir, "inbox")
    for taken in glob.glob(os.path.join(inbox, "*.taken")):
        try:
            os.remove(taken)
        except OSError:
            pass

    while True:
        if os.path.exists(os.path.join(work_dir, "shutdown")):
            return
        claimed = serve_once_api(work_dir, runs_root)
        if claimed is None:
            time.sleep(poll_interval)


# --------------------------------------------------------------------------- #
# Parallel serve (A3): consume the SAME file job channel, but run claimed jobs
# concurrently on a BoundedSubprocessPool (one in-sandbox worker subprocess per
# job, capped at ``concurrency``) instead of one-at-a-time inline. This is the
# "single-sandbox multi-process parallel execution" capability; the job channel
# protocol (inbox .ready→.taken, outbox .result.json→.done) is unchanged, so it
# is compatible with both the warm-pool and snapshot sandbox lifecycles.
# --------------------------------------------------------------------------- #

def _claim(inbox: str, ready_path: str) -> "str | None":
    """Atomically claim a ready job by renaming ``{id}.ready`` → ``{id}.taken``.
    POSIX rename is atomic, so concurrent claimers race cleanly — the loser's
    rename raises and returns None. Returns the claimed ``job_id`` or None."""
    job_id = os.path.splitext(os.path.basename(ready_path))[0]
    try:
        os.rename(ready_path, os.path.join(inbox, f"{job_id}.taken"))
        return job_id
    except OSError:
        return None


def _build_job_pool(concurrency: int, runs_root: str):
    """Build the in-sandbox worker pool over ``job_worker.py``.

    ``no_site=False`` — the worker must import the engine + api from PYTHONPATH.
    Monkeypatched in tests.
    """
    from vibecanvas_engine.subprocess_pool import BoundedSubprocessPool
    from vibecanvas_api.services.sandbox import job_worker
    return BoundedSubprocessPool(
        worker_script=job_worker.__file__,
        cwd=runs_root,
        env=dict(os.environ),
        max_workers=concurrency,
        no_site=False,
    )


def _write_result_outbox(outbox: str, job_id: str, result: dict) -> None:
    """Write ``{id}.result.json`` (atomic) BEFORE the ``{id}.done`` marker, so the
    host (which polls for .done, then reads result.json) never sees .done without
    its result alongside it."""
    res_path = os.path.join(outbox, f"{job_id}.result.json")
    tmp_res = res_path + ".tmp"
    with open(tmp_res, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.rename(tmp_res, res_path)
    done_path = os.path.join(outbox, f"{job_id}.done")
    tmp_done = done_path + ".tmp"
    with open(tmp_done, "w", encoding="utf-8") as f:
        f.write("")
    os.rename(tmp_done, done_path)


def _fileop_roots(runs_root: str) -> list[str]:
    roots_env = os.environ.get("VIBECANVAS_FILEOP_ROOTS")
    roots = [r for r in roots_env.split(":") if r] if roots_env else [runs_root]
    return roots or [runs_root]


def _get_parallel_workflow_pool(pool_holder: dict, pool_lock, concurrency: int,
                                runs_root: str):
    pool = pool_holder.get("pool")
    if pool is not None:
        return pool
    with pool_lock:
        pool = pool_holder.get("pool")
        if pool is None:
            pool = _build_job_pool(concurrency, runs_root)
            pool_holder["pool"] = pool
    return pool


class _ActivityPublisher:
    """Publish credential-free positive activity state on the /work bind.

    This is deliberately part of the existing serve loop, not another process.
    The state is an observation surface, never a TTL authority: sandboxd reads
    it and measures elapsed silence on its own monotonic clock.
    """

    def __init__(self, work_dir: str) -> None:
        self._state_path = os.path.join(work_dir, "activity.json")
        self._lock = threading.Lock()
        self._sequence = 0
        self._active_jobs = 0
        self._idle_since_ns = time.monotonic_ns()
        self._publish_locked()

    def _publish_locked(self) -> None:
        payload = {
            "version": 1,
            "pid": os.getpid(),
            "sequence": self._sequence,
            "active_jobs": self._active_jobs,
            "idle_since_monotonic_ns": (
                self._idle_since_ns if self._active_jobs == 0 else None
            ),
            "updated_monotonic_ns": time.monotonic_ns(),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".activity-", dir=os.path.dirname(self._state_path)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
            os.replace(temporary, self._state_path)
        finally:
            try:
                os.remove(temporary)
            except OSError:
                pass

    def begin(self, job_id: str) -> None:
        del job_id
        with self._lock:
            self._active_jobs += 1
            self._sequence += 1
            self._publish_locked()

    def end(self, job_id: str) -> None:
        del job_id
        with self._lock:
            self._active_jobs = max(0, self._active_jobs - 1)
            self._sequence += 1
            if self._active_jobs == 0:
                self._idle_since_ns = time.monotonic_ns()
            self._publish_locked()


def _run_one_job_to_outbox(pool_holder: dict, pool_lock, concurrency: int,
                           inbox: str, outbox: str, job_id: str,
                           runs_root: str, timeout: float,
                           activity: "_ActivityPublisher | None" = None) -> None:
    """Resolve the claimed job's descriptor → run_root, run it on ``pool``, and
    write the result + .done to the outbox. NEVER raises (a failure becomes an
    error result so the host never waits forever). Cleans up the inbox markers."""
    try:
        with open(os.path.join(inbox, f"{job_id}.json"), "r", encoding="utf-8") as f:
            desc = json.load(f)
        if desc.get("kind") == "fileop":
            from vibecanvas_api.services.sandbox.fileops import run_fileop
            result = run_fileop(desc.get("op") or {}, roots=_fileop_roots(runs_root))
        elif desc.get("kind") == "mcp":
            result = run_mcp_job(desc.get("op") or {})
        else:
            pool = _get_parallel_workflow_pool(
                pool_holder, pool_lock, concurrency, runs_root
            )
            tenant = desc.get("tenant", "")
            run_id = desc.get("run_id", "")
            sub = desc.get("run_subpath")
            # Descriptor trust boundary (N3): an escaping subpath is MALFORMED.
            if sub is not None and (os.path.isabs(sub) or ".." in sub.split(os.sep)):
                raise _MalformedRunSubpath(sub)
            run_root = (os.path.join(runs_root, sub) if sub
                        else os.path.join(runs_root, tenant, run_id))
            job = {"kind": desc.get("kind", "workflow"), "run_root": run_root,
                   "run_id": run_id, "tenant": tenant}
            result = pool.run(job, timeout)
    except Exception as e:
        result = {"ok": False, "error": str(e)}
    try:
        _write_result_outbox(outbox, job_id, result)
    except Exception:
        pass
    finally:
        for name in (f"{job_id}.taken", f"{job_id}.json"):
            try:
                os.remove(os.path.join(inbox, name))
            except OSError:
                pass
        if activity is not None:
            activity.end(job_id)


def serve_loop_parallel(work_dir: str, runs_root: str, concurrency: int,
                        poll_interval: float = 0.02,
                        timeout_per_job: float = 600.0) -> None:
    """Long-lived parallel serve loop. Claims up to ``concurrency`` ready jobs at
    a time and runs each on a worker thread that drives the BoundedSubprocessPool
    (capped at ``concurrency`` worker subprocesses). Exits on ``{work_dir}/shutdown``.

    A hung/crashed job only occupies one pool worker (the pool's per-call timeout
    SIGKILLs + respawns it), so it cannot wedge the whole loop — strictly more
    robust than the single-worker serial serve."""
    from concurrent.futures import ThreadPoolExecutor

    inbox = os.path.join(work_dir, "inbox")
    outbox = os.path.join(work_dir, "outbox")
    os.makedirs(outbox, exist_ok=True)
    # Orphan ``*.taken`` sweep on entry (a prior crash may have left claims).
    for taken in glob.glob(os.path.join(inbox, "*.taken")):
        try:
            os.remove(taken)
        except OSError:
            pass

    pool_holder: dict = {}
    pool_lock = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=concurrency)
    inflight: dict = {}   # job_id -> Future
    activity = _ActivityPublisher(work_dir)
    ready_path = os.path.join(work_dir, "ready")
    tmp_ready = ready_path + ".tmp"
    with open(tmp_ready, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    os.replace(tmp_ready, ready_path)
    try:
        while True:
            if os.path.exists(os.path.join(work_dir, "shutdown")):
                break
            for jid in [j for j, fut in list(inflight.items()) if fut.done()]:
                inflight.pop(jid, None)
            ready = sorted(glob.glob(os.path.join(inbox, "*.ready")))
            claimed_any = False
            for rp in ready:
                if len(inflight) >= concurrency:
                    break
                jid = _claim(inbox, rp)
                if jid:
                    activity.begin(jid)
                    try:
                        inflight[jid] = executor.submit(
                            _run_one_job_to_outbox,
                            pool_holder,
                            pool_lock,
                            concurrency,
                            inbox,
                            outbox,
                            jid,
                            runs_root,
                            timeout_per_job,
                            activity,
                        )
                    except Exception:
                        activity.end(jid)
                        raise
                    claimed_any = True
            # Sleep whenever this pass claimed nothing new (inbox empty OR pool at
            # capacity) so in-flight jobs are harvested each tick without busy-looping.
            if not claimed_any:
                time.sleep(poll_interval)
    finally:
        executor.shutdown(wait=True)
        _close_mcp_runtime()
        pool = pool_holder.get("pool")
        try:
            if pool is not None:
                pool.close()
        except Exception:
            pass


def main() -> None:
    # Egress proxy: start the in-sandbox forward proxy iff the provider signaled
    # "proxy" mode via env (VC_EGRESS_SOCK + VC_EGRESS_PORT). No-op in dev/host-
    # network mode. Imported from the engine package (api→engine is the allowed
    # dep direction); local import keeps the cold-import path lean. The returned
    # proxy runs on a daemon thread for the process lifetime, so it can be ignored.
    from vibecanvas_engine.egress_proxy import maybe_start_egress_proxy
    maybe_start_egress_proxy()  # no-op unless the provider signaled proxy mode

    # ``serve`` mode: long-lived warm worker over the file-job channel. run_ids
    # are uuids and never literally "serve", so this argv dispatch is safe.
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        try:
            serve_loop_api(sys.argv[2], sys.argv[3])
        finally:
            _close_mcp_runtime()
        return
    # ``serve-parallel`` mode (A3): same job channel, but run claimed jobs
    # concurrently on a BoundedSubprocessPool. argv: serve-parallel <work> <runs> <concurrency>.
    if len(sys.argv) > 1 and sys.argv[1] == "serve-parallel":
        serve_loop_parallel(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        return
    # One-shot: inside the sandbox the run-tier is bind-mounted at /run and (when
    # the host-bound execution directory. Args are just
    # [run_id, tenant].
    sys.exit(run_one("/run", sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
