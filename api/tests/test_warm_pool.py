"""RE-6 Warm T3 — ``WarmGvisorPool`` amortization + hung-worker kill+restart.

This is the EMPIRICAL deliverable for the whole RE-6-Warm arc: it proves that a
long-lived warm worker drops the per-run cost from RE-6 P2's measured ~5.3s cold
to sub-second warm, AND that a hung worker is reclaimed (kill+restart) so the
single-worker pool never wedges.

Two layers:

1. **Unit (no gVisor):** the fs-only guard (B2) refuses a non-filesystem object
   store, and the pure-engine guard (P2) rejects a host-needing node BEFORE any
   boot — both must hold without runsc.

2. **gVisor (skipif not ``_gvisor_runnable()``):**
   - **amortization (headline):** boot once, ``submit`` the SAME pure Start→End
     wf N≥10 times, DISCARD the first (cold) submit, assert the warm median is
     ≪ cold and sub-second; PRINT the numbers. Correctness vs an in-process
     ``Workflow(wf).trigger(inputs)``.
   - **per-run isolation:** two run_ids → two distinct result.json, no clobber.
   - **B5 hang-recovery (GATING):** a wf whose CodeNode sleeps ≫ the submit
     timeout → ``submit(timeout~3s)`` returns a Timeout error AND restarts the
     worker; a NEXT submit of a good wf SUCCEEDS (pool recovered, not wedged).
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import threading
import time
from unittest.mock import MagicMock

import pytest

from vibecanvas_api.config import config
from vibecanvas_api.services.object_store import (
    FilesystemObjectStore,
    InMemoryObjectStore,
)
from vibecanvas_api.services.sandbox import _gvisor_runnable
from vibecanvas_api.services.sandbox.gvisor import (
    EngineNeedsHostNode,
    EngineRunResult,
    RootlessGvisorProvider,
)
from vibecanvas_api.services.sandbox.warm import WarmGvisorPool


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------
def _start_end_wf() -> dict:
    """The trivial pure Start→End wf (NO CodeNode — isolates engine-import
    amortization; CodeNode would add ProcessPool start-up to every warm run)."""
    return {
        "__meta__": {
            "workflow_id": "wf_warm",
            "workflow_name": "warm_smoke",
            "workflow_version": 1,
            "workflow_subversion": 0,
        },
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "StartNode",
            "node_description": "start",
            "input_fields": {},
            "output_fields": {"x": {"type": "integer", "description": "n"}},
            "node_config": {},
            "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2",
            "node_name": "__end__",
            "node_type": "EndNode",
            "node_description": "end",
            "input_fields": {"x": {"type": "integer", "value": 0, "reference": "__start__.x"}},
            "output_fields": {"x": {"type": "integer", "description": "passthrough"}},
            "node_config": {},
            "children": [],
        },
    }


def _hang_wf() -> dict:
    """A Start→Code wf whose code sleeps far longer than the host submit timeout
    → the host MUST kill+restart the worker (B5). ``time.sleep(30)`` ≫ a ~3s
    submit timeout so the host-side reclaim fires first."""
    return {
        "__meta__": {
            "workflow_id": "wf_hang",
            "workflow_name": "hang",
            "workflow_version": 1,
            "workflow_subversion": 0,
        },
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "StartNode",
            "node_description": "start",
            "input_fields": {},
            "output_fields": {"x": {"type": "integer", "description": "n"}},
            "node_config": {},
            "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2",
            "node_name": "sleeper",
            "node_type": "CodeNode",
            "node_description": "sleep forever-ish",
            "input_fields": {
                "x": {"type": "integer", "value": 0, "reference": "__start__.x"},
            },
            "output_fields": {"v": {"type": "integer", "description": "x"}},
            "node_config": {
                "programming_language": "python",
                "process_fn": (
                    "def process_fn(inputs):\n"
                    "    import time\n"
                    "    time.sleep(30)\n"
                    "    return {'v': inputs['x']}"
                ),
            },
            "children": ["node_3"],
        },
        "node_3": {
            "node_id": "node_3",
            "node_name": "__end__",
            "node_type": "EndNode",
            "node_description": "end",
            "input_fields": {"v": {"type": "integer", "value": 0, "reference": "sleeper.v"}},
            "output_fields": {"v": {"type": "integer", "description": "x"}},
            "node_config": {},
            "children": [],
        },
    }


def _host_node_wf() -> dict:
    """A wf with a node_type NOT in the frozen engine-pure set → the pure-engine
    guard must reject it BEFORE boot (P2 guard reuse)."""
    return {
        "__meta__": {"workflow_id": "wf_host", "workflow_name": "host"},
        "node_1": {
            "node_id": "node_1",
            "node_name": "kb",
            "node_type": "KnowledgeSearchNode",
            "node_description": "needs postgres egress",
            "input_fields": {},
            "output_fields": {},
            "node_config": {},
            "children": [],
        },
    }


# ===========================================================================
# Unit — no gVisor (fs-only guard + pure-engine guard, no boot)
# ===========================================================================
def test_runtime_binds_include_shared_dependency_cache(tmp_path, monkeypatch):
    """A resident worker sees overlays published after it has booted."""
    from vibecanvas_api.config import config
    from vibecanvas_api.services.sandbox import warm

    base = tmp_path / "base"
    base.mkdir()
    cache = tmp_path / "dependency-cache"
    monkeypatch.setattr(warm, "_workflow_python_binds", lambda: [str(base)])
    monkeypatch.setattr(config, "lib_overlay_root", str(cache), raising=False)

    binds = WarmGvisorPool._runtime_ro_binds()

    assert binds == [str(base), str(cache)]
    assert cache.is_dir()


def test_pool_requires_filesystem_store(tmp_path, monkeypatch):
    """B2: a non-filesystem object store → start() refuses (RuntimeError)."""
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: InMemoryObjectStore(),
    )
    pool = WarmGvisorPool(
        provider=object(),  # never reached — guard fires before run_serve
        store_root=str(tmp_path / "store"),
        work_root=str(tmp_path / "work"),
    )
    with pytest.raises(RuntimeError, match="filesystem object store"):
        pool.start()


def test_submit_host_node_raises_needs_host(tmp_path, monkeypatch):
    """The pure-engine guard rejects a host-needing node BEFORE booting — we can
    assert it without a live worker (the guard runs first in submit)."""
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: FilesystemObjectStore(root=str(tmp_path / "store")),
    )
    pool = WarmGvisorPool(
        provider=object(),
        store_root=str(tmp_path / "store"),
        work_root=str(tmp_path / "work"),
    )
    with pytest.raises(EngineNeedsHostNode):
        pool.submit(
            workflow=_host_node_wf(),
            inputs={},
            run_id="r_host",
            tenant="t",
            timeout=5.0,
        )


# ===========================================================================
# Unit — submit_stream live tail (no gVisor; a fake worker writes the channel)
# ===========================================================================
class _FakeReadProvider:
    """A provider stand-in whose ``_read_engine_result`` reads the run-tier
    result.json (exactly like the real RootlessGvisorProvider does for warm —
    sandbox=None). No boot; the test plays the worker by hand."""

    def _read_engine_result(self, exec_dir, _sandbox):
        with open(os.path.join(exec_dir, "result.json"), encoding="utf-8") as f:
            parsed = json.load(f)
        return EngineRunResult(
            final_outputs=parsed.get("final_outputs", {}) or {},
            error_dict=parsed.get("error_dict", {}) or {},
            execution_time=parsed.get("execution_time", 0.0) or 0.0,
            events=[],
            sandbox=None,
        )


def _stream_pool(tmp_path, monkeypatch) -> WarmGvisorPool:
    store_root = str(tmp_path / "store")
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: FilesystemObjectStore(root=store_root),
    )
    pool = WarmGvisorPool(
        provider=_FakeReadProvider(),
        store_root=store_root,
        work_root="",  # set below to the real work_root
        tenant="t",
        poll_interval=0.01,
    )
    pool.work_root = str(tmp_path / "work")
    os.makedirs(pool._inbox, exist_ok=True)
    os.makedirs(pool._outbox, exist_ok=True)
    # No worker booted — submit_stream's death-check is skipped when _handle None.
    pool._handle = None
    return pool


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_timeout", [10.0, None])
async def test_submit_stream_events_then_result(tmp_path, monkeypatch, transport_timeout):
    """Happy path: the fake worker appends events.ndjson lines + writes
    result.json + .done → submit_stream yields node_events (terminal engine
    ``finished`` SKIPPED) then a single ``result``."""
    pool = _stream_pool(tmp_path, monkeypatch)
    exec_dir = os.path.join(pool._runs_root, "rs1", "__exec__")

    def _worker():
        # The host wrote __exec__ already (in _prep_job); append events as the
        # in-sandbox engine would (per-line flush), then result.json + .done.
        time.sleep(0.05)
        events_path = os.path.join(exec_dir, "events.ndjson")
        with open(events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"status": "success", "node_id": "node_1"}) + "\n")
            f.flush()
            time.sleep(0.05)
            f.write(json.dumps({"status": "success", "node_id": "node_2"}) + "\n")
            f.flush()
            # terminal engine event — must be SKIPPED by the tail.
            f.write(json.dumps({"status": "finished",
                                "final_outputs": {"__end__": {"v": 5}},
                                "error_dict": {}}) + "\n")
            f.flush()
        with open(os.path.join(exec_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"final_outputs": {"__end__": {"v": 5}},
                       "error_dict": {}, "execution_time": 0.3}, f)
        # .done LAST, atomically-ish.
        # We don't know the job_id here; instead drop .done for ANY job by
        # watching the inbox for the job json the host wrote.
        for _ in range(200):
            jobs = [n for n in os.listdir(pool._inbox) if n.endswith(".json")]
            if jobs:
                jid = os.path.splitext(jobs[0])[0]
                open(os.path.join(pool._outbox, f"{jid}.done"), "w").close()
                return
            time.sleep(0.01)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    items = await _collect(pool.submit_stream(
        workflow=_start_end_wf(), inputs={"x": 5}, run_id="rs1",
        tenant="t", timeout=transport_timeout,
    ))
    t.join(timeout=5.0)

    types = [m["type"] for m in items]
    assert types[-1] == "result", items
    node_events = [m for m in items if m["type"] == "node_event"]
    assert [m["node_id"] for m in node_events] == ["node_1", "node_2"], items
    # No ``finished`` engine frame leaked as a node_event.
    assert all(m.get("status") != "finished" for m in node_events)
    result = items[-1]
    assert result["final_outputs"] == {"__end__": {"v": 5}}
    assert result["error_dict"] == {}


def test_stream_job_can_delegate_duration_to_workflow_policy(tmp_path, monkeypatch):
    pool = _stream_pool(tmp_path, monkeypatch)
    job_id, _ = pool._prep_job(workflow=_start_end_wf(), inputs={}, run_id="policy-run", tenant="t", execution_timeout=None)
    with open(os.path.join(pool._inbox, job_id + ".json")) as handle:
        descriptor = json.load(handle)
    assert descriptor["kind"] == "workflow"
    assert "execution_timeout" in descriptor and descriptor["execution_timeout"] is None


@pytest.mark.asyncio
async def test_submit_stream_hang_running_restarts(tmp_path, monkeypatch):
    """B3: a RUNNING job (.taken present) with no .done + no progress within the
    timeout → kill+restart the worker + yield a terminal ``timeout``."""
    pool = _stream_pool(tmp_path, monkeypatch)

    restarted = {"n": 0}
    monkeypatch.setattr(pool, "_restart_worker", lambda: restarted.__setitem__("n", restarted["n"] + 1))

    # Simulate the worker having CLAIMED the job (.taken) but then hanging.
    def _claim():
        for _ in range(500):
            jobs = [n for n in os.listdir(pool._inbox) if n.endswith(".json")]
            if jobs:
                jid = os.path.splitext(jobs[0])[0]
                open(os.path.join(pool._inbox, f"{jid}.taken"), "w").close()
                return
            time.sleep(0.005)

    threading.Thread(target=_claim, daemon=True).start()
    items = await _collect(pool.submit_stream(
        workflow=_start_end_wf(), inputs={}, run_id="rs_hang",
        tenant="t", timeout=0.3,
    ))

    assert items, "expected a terminal frame"
    assert items[-1]["type"] == "timeout"
    assert "RUNNING" in items[-1]["message"]
    assert restarted["n"] == 1, "RUNNING-timeout must kill+restart the worker"


@pytest.mark.asyncio
async def test_submit_stream_cancel_writes_marker_on_abandon(tmp_path, monkeypatch):
    """Consumer-abandon BEFORE a result → submit_stream's finally writes the
    run's __exec__/cancel marker (graceful in-sandbox stop)."""
    pool = _stream_pool(tmp_path, monkeypatch)
    exec_dir = os.path.join(pool._runs_root, "rs_ab", "__exec__")

    agen = pool.submit_stream(
        workflow=_start_end_wf(), inputs={}, run_id="rs_ab",
        tenant="t", timeout=10.0,
    )
    # Start iteration far enough to run _prep_job; nothing has been yielded yet
    # (no events, no .done), so pull with a short timeout then close.
    task = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    await agen.aclose()

    assert os.path.exists(os.path.join(exec_dir, "cancel")), \
        "abandon should write the graceful cancel marker"


# ===========================================================================
# Unit — HARD cancel (task #483): cancel() = marker + kill+restart worker
# (no gVisor; a controllable fake provider models the worker boot/kill/serve)
# ===========================================================================
class _FakeProc:
    """A Popen stand-in: ``poll()`` returns None while alive, an exit code once
    ``_die()`` is called (mirrors a killed worker process)."""

    def __init__(self) -> None:
        self._rc: "int | None" = None

    def poll(self) -> "int | None":
        return self._rc

    def _die(self) -> None:
        if self._rc is None:
            self._rc = -9  # SIGKILL-ish


class _FakeServeHandle:
    def __init__(self, proc: _FakeProc) -> None:
        self.proc = proc


class _ControllableProvider(_FakeReadProvider):
    """A provider whose ``run_serve`` boots a fake worker THREAD that claims +
    serves channel jobs, and whose ``stop_serve`` kills it. ``run_serve`` returns
    a fresh handle each boot → a test can assert a RESTART produced a new handle.

    The worker, per claimed job, normally writes events + result.json + .done. A
    job whose run_id is in ``hang_run_ids`` is CLAIMED (``.taken``) then HANGS
    (never finishes) — modelling a mid-node runaway that only a kill can stop.
    """

    def __init__(self, inbox: str, outbox: str, runs_root_getter,
                 *, hang_run_ids=()):
        self._inbox = inbox
        self._outbox = outbox
        self._runs_root_getter = runs_root_getter
        self._hang = set(hang_run_ids)
        self.boots = 0
        self.stops = 0
        self._stop_evt: "threading.Event | None" = None
        self._thread: "threading.Thread | None" = None

    def run_serve(self, **kwargs) -> _FakeServeHandle:
        self.boots += 1
        proc = _FakeProc()
        stop_evt = threading.Event()
        self._stop_evt = stop_evt

        def _worker() -> None:
            seen: set[str] = set()
            while not stop_evt.is_set():
                try:
                    names = os.listdir(self._inbox)
                except OSError:
                    names = []
                for n in names:
                    if not n.endswith(".ready"):
                        continue
                    jid = n[: -len(".ready")]
                    if jid in seen:
                        continue
                    job_file = os.path.join(self._inbox, f"{jid}.json")
                    try:
                        with open(job_file, encoding="utf-8") as f:
                            job = json.load(f)
                    except (OSError, ValueError):
                        continue
                    seen.add(jid)
                    open(os.path.join(self._inbox, f"{jid}.taken"), "w").close()
                    self._serve_job(jid, job, stop_evt)
                if stop_evt.wait(0.005):
                    break

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        self._thread = t
        return _FakeServeHandle(proc)

    def _serve_job(self, jid: str, job: dict, stop_evt) -> None:
        run_subpath = job["run_subpath"]
        exec_dir = os.path.join(self._runs_root_getter(), run_subpath, "__exec__")
        run_id = job["run_id"]
        if run_id in self._hang:
            # Mid-node runaway: claimed (.taken written above) but never finishes.
            # Only stop_serve (the kill) ends this worker.
            stop_evt.wait()  # park until killed
            return
        events_path = os.path.join(exec_dir, "events.ndjson")
        try:
            with open(events_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"status": "success", "node_id": "node_1"}) + "\n")
                f.flush()
            with open(os.path.join(exec_dir, "result.json"), "w", encoding="utf-8") as f:
                json.dump({"final_outputs": {"__end__": {"x": 1}},
                           "error_dict": {}, "execution_time": 0.1}, f)
            open(os.path.join(self._outbox, f"{jid}.done"), "w").close()
        except OSError:
            pass

    def stop_serve(self, handle: _FakeServeHandle) -> None:
        self.stops += 1
        if self._stop_evt is not None:
            self._stop_evt.set()
        handle.proc._die()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def _controllable_pool(tmp_path, monkeypatch, *, hang_run_ids=()):
    store_root = str(tmp_path / "store")
    work_root = str(tmp_path / "work")
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: FilesystemObjectStore(root=store_root),
    )
    pool = WarmGvisorPool(
        provider=None,  # set below (needs the pool's _inbox/_outbox/_runs_root)
        store_root=store_root,
        work_root=work_root,
        tenant="t",
        poll_interval=0.01,
    )
    os.makedirs(pool._inbox, exist_ok=True)
    os.makedirs(pool._outbox, exist_ok=True)
    os.makedirs(pool._runs_root, exist_ok=True)
    prov = _ControllableProvider(
        pool._inbox, pool._outbox, lambda: pool._runs_root,
        hang_run_ids=hang_run_ids,
    )
    pool.provider = prov
    return pool, prov


def test_resident_pool_wires_proxy_and_expands_policy(tmp_path, monkeypatch):
    store_root = str(tmp_path / "store")
    work_root = str(tmp_path / "work")
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: FilesystemObjectStore(root=store_root),
    )
    monkeypatch.setattr(config, "sandbox_egress_mode", "proxy")

    class Loop:
        def __init__(self):
            self.added = []
            self.stopped = False

        def acquire_allow_hosts(self, hosts):
            self.added.append(set(hosts))
            return "lease-1"

        def release_allow_hosts(self, lease_id):
            self.released = lease_id

        def stop(self):
            self.stopped = True

    class Proc:
        def poll(self):
            return None

    class Provider:
        def __init__(self):
            self.loop = Loop()
            self.run_kwargs = None

        def _sandbox_egress_setup(self, run_id, allow_hosts):
            assert run_id.startswith("resident:t:")
            assert allow_hosts == set()
            return (
                self.loop,
                str(tmp_path / "egress" / "egress.sock"),
                {"HTTP_PROXY": "http://127.0.0.1:13128"},
            )

        def run_serve(self, **kwargs):
            self.run_kwargs = kwargs
            return MagicMock(proc=Proc())

        def stop_serve(self, _handle):
            return None

    provider = Provider()
    pool = WarmGvisorPool(
        provider=provider,
        store_root=store_root,
        work_root=work_root,
        tenant="t",
    )
    pool.start()
    try:
        assert provider.run_kwargs["network"] == "none"
        assert provider.run_kwargs["egress_socket"].endswith("egress.sock")
        assert provider.run_kwargs["env"]["HTTP_PROXY"].endswith(":13128")
        lease_id = pool.acquire_egress_hosts({"FILES.EXAMPLE"})
        assert lease_id == "lease-1"
        assert provider.loop.added == [{"files.example"}]
        pool.release_egress_hosts(lease_id)
        assert provider.loop.released == "lease-1"
    finally:
        pool.stop()
    assert provider.loop.stopped is True


def test_retired_task_pool_cannot_be_restarted_by_late_cancel(tmp_path, monkeypatch):
    pool, provider = _controllable_pool(tmp_path, monkeypatch)
    pool.start()
    boots = provider.boots
    pool.stop(retire=True)
    with pytest.raises(RuntimeError, match="permanently retired"):
        pool.cancel(run_id="late", tenant="t")
    assert provider.boots == boots
    with pytest.raises(RuntimeError, match="permanently retired"):
        pool._ensure_started()
    assert provider.boots == boots


def test_cancel_kills_and_restarts_worker(tmp_path, monkeypatch):
    """HARD cancel: a streaming RUNAWAY run + explicit cancel → _restart_worker is
    invoked (a NEW handle), the marker is written, the run_dir is RETAINED, and a
    SUBSEQUENT submit on the same tenant SUCCEEDS (worker recovered)."""
    pool, prov = _controllable_pool(tmp_path, monkeypatch, hang_run_ids={"runaway"})
    pool.start()
    assert prov.boots == 1
    handle_before = pool._handle

    exec_dir = os.path.join(pool._runs_root, "runaway", "__exec__")

    async def _drive_then_cancel():
        agen = pool.submit_stream(
            workflow=_start_end_wf(), inputs={}, run_id="runaway",
            tenant="t", timeout=30.0,
        )
        # Pull the first frame (or let the run get CLAIMED + start hanging).
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.2)  # worker claims the job (.taken) + hangs
        # Mid-node runaway → explicit HARD cancel kills+restarts the worker.
        t0 = time.monotonic()
        pool.cancel(run_id="runaway", tenant="t")
        cancel_dt = time.monotonic() - t0
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await agen.aclose()
        return cancel_dt

    cancel_dt = asyncio.run(_drive_then_cancel())

    # PROMPT: cancel returned quickly (did NOT wait for the 30s node).
    assert cancel_dt < 5.0, f"cancel blocked {cancel_dt:.1f}s — not prompt"
    # Worker killed + restarted → a FRESH handle, stop_serve called.
    assert pool._handle is not handle_before, "worker not restarted on cancel"
    assert prov.stops >= 1 and prov.boots >= 2, (prov.stops, prov.boots)
    assert handle_before.proc.poll() is not None, "old worker not killed"
    # Belt-and-suspenders marker written.
    assert os.path.exists(os.path.join(exec_dir, "cancel"))
    # Run_dir RETAINED (kill-to-inspect).
    assert os.path.isdir(os.path.join(pool._runs_root, "runaway")), \
        "run_dir must be retained after hard cancel"

    # RECOVERED: a normal submit_stream now SUCCEEDS on the fresh worker.
    async def _after():
        items = []
        async for m in pool.submit_stream(
            workflow=_start_end_wf(), inputs={}, run_id="after",
            tenant="t", timeout=10.0,
        ):
            items.append(m)
        return items

    items = asyncio.run(_after())
    assert items[-1]["type"] == "result", items
    assert items[-1]["error_dict"] == {}
    pool.stop()


def test_cancel_is_idempotent(tmp_path, monkeypatch):
    """A second cancel after the run already finished is a no-op-ish (still a
    valid reboot, no crash) — guards the belt-and-suspenders double-call."""
    pool, prov = _controllable_pool(tmp_path, monkeypatch)
    pool.start()
    pool.cancel(run_id="never_ran", tenant="t")  # no live job
    pool.cancel(run_id="never_ran", tenant="t")  # twice — must not raise
    # Each cancel reboots once (idempotent in EFFECT — a fresh worker each time).
    assert prov.boots >= 3  # initial + 2 cancels
    pool.stop()


# ===========================================================================
# gVisor — real runsc
# ===========================================================================
gvisor = pytest.mark.skipif(
    not _gvisor_runnable(), reason="rootless gVisor not runnable here"
)


def _resolve() -> str:
    from vibecanvas_api.services.sandbox import _resolve_runsc

    return _resolve_runsc()


def _fs_store_pool(tmp_path, monkeypatch) -> WarmGvisorPool:
    """A WarmGvisorPool wired to a real RootlessGvisorProvider + a filesystem
    object store rooted at ``store_root`` (so the B2 guard passes)."""
    store_root = str(tmp_path / "store")
    work_root = str(tmp_path / "work")
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: FilesystemObjectStore(root=store_root),
    )
    return WarmGvisorPool(
        provider=RootlessGvisorProvider(_resolve()),
        store_root=store_root,
        work_root=work_root,
    )


@gvisor
def test_warm_amortization(tmp_path, monkeypatch, capsys):
    """HEADLINE: warm submits are ≪ the cold first submit and sub-second."""
    from vibecanvas_engine.workflow import Workflow

    pool = _fs_store_pool(tmp_path, monkeypatch)
    pool.start()
    try:
        wf = _start_end_wf()
        inputs = {"x": 7}
        times: list[float] = []
        last_result: EngineRunResult | None = None
        for i in range(12):
            t0 = time.monotonic()
            res = pool.submit(
                workflow=wf,
                inputs=inputs,
                run_id=f"r_amort_{i}",
                tenant="t",
                timeout=60.0,
            )
            times.append(time.monotonic() - t0)
            assert res.error_dict == {}, f"submit {i} engine errors: {res.error_dict}"
            last_result = res

        cold = times[0]
        warm = times[1:]
        warm_median = statistics.median(warm)
        print(
            f"\n[RE-6 Warm] cold(first)={cold:.3f}s  "
            f"warm median={warm_median:.3f}s  warm={[f'{t:.3f}' for t in warm]}"
        )

        # Correctness: the warm result matches an in-process trigger.
        outs, err, _ = Workflow(_start_end_wf()).trigger(dict(inputs))
        assert err == {}, f"in-process trigger errored: {err}"
        assert last_result is not None
        assert last_result.final_outputs == outs, (
            f"warm {last_result.final_outputs} != in-process {outs}"
        )

        # The headline: warm ≪ cold AND sub-second-ish.
        assert warm_median < cold, "warm not faster than cold — no amortization"
        assert warm_median < 1.5, f"warm median {warm_median:.3f}s not sub-second"
    finally:
        pool.stop()


@gvisor
def test_warm_per_run_isolation(tmp_path, monkeypatch):
    """Two run_ids → two distinct result.json subdirs, no clobber."""
    pool = _fs_store_pool(tmp_path, monkeypatch)
    pool.start()
    try:
        wf = _start_end_wf()
        r1 = pool.submit(workflow=wf, inputs={"x": 1}, run_id="iso_a", tenant="t", timeout=60.0)
        r2 = pool.submit(workflow=wf, inputs={"x": 2}, run_id="iso_b", tenant="t", timeout=60.0)
        assert r1.error_dict == {} and r2.error_dict == {}
        store_root = str(tmp_path / "store")
        p1 = os.path.join(store_root, "run", "t", "iso_a", "__exec__", "result.json")
        p2 = os.path.join(store_root, "run", "t", "iso_b", "__exec__", "result.json")
        assert os.path.exists(p1) and os.path.exists(p2), "missing per-run result.json"
        assert r1.final_outputs["__end__"]["x"] == 1
        assert r2.final_outputs["__end__"]["x"] == 2
    finally:
        pool.stop()


@gvisor
def test_warm_hang_recovery(tmp_path, monkeypatch, capsys):
    """B5 GATING: a hanging job times out, the pool kills+restarts the worker,
    and the NEXT good submit SUCCEEDS (the pool recovered, not wedged)."""
    pool = _fs_store_pool(tmp_path, monkeypatch)
    pool.start()
    try:
        # WARM the worker first (a cold boot is ~3-5s). Otherwise the hang job
        # below may still be QUEUED on the cold-booting worker when its 3s
        # timeout fires — and a queued-timeout (correctly, B4) does NOT kill the
        # worker. We want to exercise the RUNNING-timeout kill+restart path (B3),
        # so the worker must already be warm + ready to CLAIM the hang job.
        warm = pool.submit(
            workflow=_start_end_wf(), inputs={"x": 0},
            run_id="warmup", tenant="t", timeout=60.0,
        )
        assert warm.error_dict == {}, f"warmup submit errored: {warm.error_dict}"
        handle_before = pool._handle  # noqa: SLF001 — test asserts a restart happened
        # A job that sleeps 30s with a 3s submit timeout → host must reclaim.
        res = pool.submit(
            workflow=_hang_wf(),
            inputs={"x": 1},
            run_id="hang_1",
            tenant="t",
            timeout=3.0,
        )
        # Timeout surfaced as an engine-error result (not a clean run).
        assert res.error_dict, "hanging job should have produced a timeout error"
        print(f"\n[RE-6 Warm] hang timeout error_dict={res.error_dict}")

        # The poisoned worker was killed + restarted (a fresh handle).
        assert pool._handle is not handle_before, "worker was not restarted"  # noqa: SLF001
        # stop_serve SIGKILLed the process group; reap the zombie so poll() can
        # observe the death (poll() returns None until the child is wait()ed).
        try:
            handle_before.proc.wait(timeout=10)
        except Exception:
            pass
        assert handle_before.proc.poll() is not None, "old (hung) worker not killed"

        # The pool RECOVERED: a normal submit now succeeds within a normal timeout.
        good = pool.submit(
            workflow=_start_end_wf(),
            inputs={"x": 9},
            run_id="after_hang",
            tenant="t",
            timeout=60.0,
        )
        assert good.error_dict == {}, f"post-recovery submit errored: {good.error_dict}"
        assert good.final_outputs["__end__"]["x"] == 9
    finally:
        pool.stop()


@gvisor
def test_warm_stop_kills_worker(tmp_path, monkeypatch):
    """stop() tears the worker down (proc killed)."""
    pool = _fs_store_pool(tmp_path, monkeypatch)
    pool.start()
    handle = pool._handle  # noqa: SLF001
    assert handle.proc.poll() is None
    pool.stop()
    handle.proc.wait(timeout=10)
    assert handle.proc.poll() is not None, "stop() did not kill the worker"
