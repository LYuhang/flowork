# -*- coding: utf-8 -*-
"""Per-run CodeWorkerPool lifecycle: lazy creation, reuse, and final teardown.
teardown, and cancel-kill.

These tests drive real workflows through ``Workflow.astream`` (the per-run pool
spawns host ``code_worker`` subprocesses — no gVisor needed). They pin:

  * ONE pool per run, reused across CodeNodes and loop iterations (worker reuse,
    not fresh-per-call).
  * Parallel branches each run their CodeNode CONCURRENTLY on the run's pool
    (wall-clock ≈ one sleep, not N sleeps) with correct per-branch outputs.
  * The pool is torn down at run end — on success, on error, and on cancel —
    with no orphan worker subprocesses.
"""

from __future__ import annotations

import asyncio
import time

import psutil
import pytest

from vibecanvas_engine.workflow import Workflow
from vibecanvas_engine import code_runner


# --------------------------------------------------------------------------- #
# workflow builders
# --------------------------------------------------------------------------- #
def _start(children):
    return {
        "node_id": "node_1",
        "node_name": "__start__",
        "node_type": "StartNode",
        "node_description": "",
        "input_fields": {},
        "output_fields": {},
        "node_config": {},
        "children": children,
    }


def _code(node_id, name, process_fn, children, inputs=None, outputs=None):
    return {
        "node_id": node_id,
        "node_name": name,
        "node_type": "CodeNode",
        "node_description": "",
        "input_fields": inputs or {},
        "output_fields": outputs or {"v": {"type": "integer", "description": ""}},
        "node_config": {
            "programming_language": "python",
            "process_fn": process_fn,
        },
        "children": children,
    }


def _end(node_id, inputs=None):
    return {
        "node_id": node_id,
        "node_name": "__end__",
        "node_type": "EndNode",
        "node_description": "",
        "input_fields": inputs or {},
        "output_fields": {},
        "node_config": {},
        "children": [],
    }


def _three_codenode_wf() -> dict:
    """Start → Code → Code → Code → End (3 sequential CodeNodes)."""
    fn = "def process_fn(inputs):\n    return {'v': 1}"
    return {
        "__meta__": {"workflow_id": "wf_3code", "workflow_name": "three", "workflow_version": 1, "workflow_subversion": 0},
        "node_1": _start(["node_2"]),
        "node_2": _code("node_2", "c1", fn, ["node_3"]),
        "node_3": _code("node_3", "c2", fn, ["node_4"]),
        "node_4": _code("node_4", "c3", fn, ["node_5"]),
        "node_5": _end("node_5"),
    }


def _loop_codenode_wf(n: int = 5) -> dict:
    """Start → LoopBegin(0..n) → Code → LoopEnd → End (Code runs n+1 times)."""
    fn = "def process_fn(inputs):\n    return {'v': 1}"
    return {
        "__meta__": {"workflow_id": "wf_loopcode", "workflow_name": "loopcode", "workflow_version": 1, "workflow_subversion": 0},
        "node_1": _start(["node_2"]),
        "node_2": {
            "node_id": "node_2", "node_name": "lb", "node_type": "LoopBeginNode",
            "node_description": "", "input_fields": {},
            "output_fields": {"i": {"type": "integer", "description": ""}},
            "node_config": {
                "init_value": {"value": 0, "reference": ""},
                "end_value": {"value": n, "reference": ""},
                "step_value": 1, "loop_end_node_id": "node_4",
            },
            "children": ["node_3"],
        },
        "node_3": _code("node_3", "body", fn, ["node_4"]),
        "node_4": {
            "node_id": "node_4", "node_name": "le", "node_type": "LoopEndNode",
            "node_description": "", "input_fields": {}, "output_fields": {},
            "node_config": {"loop_begin_node_id": "node_2"}, "children": ["node_5"],
        },
        "node_5": _end("node_5"),
    }


def _parallel_sleep_wf(sleep: float = 0.5) -> dict:
    """Start → ParallelStart → (A sleeps, B sleeps) → ParallelEnd → End.

    Each branch CodeNode records its sleep WINDOW (start/end wall-clock) into a
    file in the run dir, sleeps ``sleep``, then echoes its own branch tag. The
    test asserts the two windows OVERLAP (concurrent execution) — robust against
    this environment's multi-second Python interpreter cold-start, which makes a
    raw end-to-end wall-clock threshold flaky (a serial run would NOT overlap).
    """
    fn_a = (
        "def process_fn(inputs):\n"
        "    import time, os, json\n"
        "    t0 = time.time()\n"
        "    time.sleep(%(s)s)\n"
        "    t1 = time.time()\n"
        "    with open(os.path.join(os.getcwd(), 'win_A.json'), 'w') as f:\n"
        "        json.dump([t0, t1], f)\n"
        "    return {'y': inputs['x'] + '-A'}"
    ) % {"s": sleep}
    fn_b = (
        "def process_fn(inputs):\n"
        "    import time, os, json\n"
        "    t0 = time.time()\n"
        "    time.sleep(%(s)s)\n"
        "    t1 = time.time()\n"
        "    with open(os.path.join(os.getcwd(), 'win_B.json'), 'w') as f:\n"
        "        json.dump([t0, t1], f)\n"
        "    return {'y': inputs['x'] + '-B'}"
    ) % {"s": sleep}
    return {
        "__meta__": {"workflow_id": "wf_par_sleep", "workflow_name": "par", "workflow_version": 1, "workflow_subversion": 0},
        "node_1": {
            "node_id": "node_1", "node_name": "__start__", "node_type": "StartNode",
            "node_description": "", "input_fields": {"x": {"type": "string", "value": "", "reference": ""}},
            "output_fields": {"x": {"type": "string", "description": ""}},
            "node_config": {}, "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2", "node_name": "psplit", "node_type": "ParallelStartNode",
            "node_description": "", "input_fields": {}, "output_fields": {},
            "node_config": {
                "branches": {
                    "a": {"branch_description": "a", "next_node_id": "node_3"},
                    "b": {"branch_description": "b", "next_node_id": "node_4"},
                },
                "parallel_end_node_id": "node_5",
            },
            "children": ["node_3", "node_4"],
        },
        "node_3": _code(
            "node_3", "branch_a", fn_a, ["node_5"],
            inputs={"x": {"type": "string", "value": "", "reference": "__start__.x"}},
            outputs={"y": {"type": "string", "description": ""}},
        ),
        "node_4": _code(
            "node_4", "branch_b", fn_b, ["node_5"],
            inputs={"x": {"type": "string", "value": "", "reference": "__start__.x"}},
            outputs={"y": {"type": "string", "description": ""}},
        ),
        "node_5": {
            "node_id": "node_5", "node_name": "pmerge", "node_type": "ParallelEndNode",
            "node_description": "", "input_fields": {}, "output_fields": {},
            "node_config": {"parallel_start_node_id": "node_2"}, "children": ["node_6"],
        },
        "node_6": _end("node_6", inputs={
            "a": {"type": "string", "value": "", "reference": "branch_a.y"},
            "b": {"type": "string", "value": "", "reference": "branch_b.y"},
        }),
    }


def _error_then_code_wf() -> dict:
    """Start → Code(raises) → Code → End. The first CodeNode errors mid-run; the
    pool was created and must still be torn down at run end."""
    boom = "def process_fn(inputs):\n    raise ValueError('boom')"
    ok = "def process_fn(inputs):\n    return {'v': 1}"
    return {
        "__meta__": {"workflow_id": "wf_err", "workflow_name": "err", "workflow_version": 1, "workflow_subversion": 0},
        "node_1": _start(["node_2"]),
        "node_2": _code("node_2", "boom", boom, ["node_3"]),
        "node_3": _code("node_3", "after", ok, ["node_4"]),
        "node_4": _end("node_4"),
    }


def _long_code_wf(sleep: float = 30.0) -> dict:
    """Start → Code(sleeps a long time) → End. Used to cancel mid-run."""
    fn = (
        "def process_fn(inputs):\n"
        "    import time\n"
        f"    time.sleep({sleep})\n"
        "    return {'v': 1}"
    )
    return {
        "__meta__": {"workflow_id": "wf_long", "workflow_name": "long", "workflow_version": 1, "workflow_subversion": 0},
        "node_1": _start(["node_2"]),
        "node_2": _code("node_2", "slow", fn, ["node_3"]),
        "node_3": _end("node_3"),
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _child_code_workers() -> list[psutil.Process]:
    """Return live ``code_worker`` subprocesses of THIS process (orphan check)."""
    me = psutil.Process()
    out = []
    for c in me.children(recursive=True):
        try:
            if any("code_worker" in part for part in c.cmdline()):
                out.append(c)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return out


class _SpyPool(code_runner.CodeWorkerPool):
    """A CodeWorkerPool that counts worker spawns and remembers the last instance."""

    instances: list = []
    spawn_count: int = 0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        type(self).instances.append(self)

    def _spawn(self):
        type(self).spawn_count += 1
        return super()._spawn()


@pytest.fixture
def spy_pool(monkeypatch):
    _SpyPool.instances = []
    _SpyPool.spawn_count = 0
    monkeypatch.setattr("vibecanvas_engine.nodes.code.CodeWorkerPool", _SpyPool)
    return _SpyPool


# --------------------------------------------------------------------------- #
# 1. reuse across CodeNodes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pool_reused_across_codenodes(spy_pool, tmp_path):
    wf = Workflow(_three_codenode_wf(), max_workers=4)
    finished = None
    async for ev in wf.astream({}, run_context={"run_id": "r", "run_dir": str(tmp_path)}):
        if ev.get("status") == "finished":
            finished = ev
    assert finished is not None and not finished.get("error_dict"), finished

    # Exactly ONE pool created for the whole run.
    assert len(spy_pool.instances) == 1, f"expected 1 pool, got {len(spy_pool.instances)}"
    # 3 sequential CodeNodes reuse the SAME worker → exactly 1 spawn, not 3.
    assert spy_pool.spawn_count == 1, (
        f"expected worker reuse (1 spawn), got {spy_pool.spawn_count}"
    )


# --------------------------------------------------------------------------- #
# 2. loop reuse
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_loop_codenode_reuses_pool(spy_pool, tmp_path):
    n = 5
    wf = Workflow(_loop_codenode_wf(n), max_workers=4)
    finished = None
    async for ev in wf.astream({}, run_context={"run_id": "r", "run_dir": str(tmp_path)}):
        if ev.get("status") == "finished":
            finished = ev
    assert finished is not None and not finished.get("error_dict"), finished

    assert len(spy_pool.instances) == 1, f"expected 1 pool, got {len(spy_pool.instances)}"
    # The CodeNode runs n+1 times sequentially → still only ONE worker spawned
    # (amortized reuse), NOT a fresh worker per iteration.
    assert spy_pool.spawn_count == 1, (
        f"loop should reuse one worker; got {spy_pool.spawn_count} spawns for {n + 1} iterations"
    )


# --------------------------------------------------------------------------- #
# 3. parallel concurrency (the key one)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cold_parallel_runs_release_code_workers(spy_pool, tmp_path):
    """No injected pool: branch-local creation must not escape run cleanup."""
    before = {p.pid for p in _child_code_workers()}
    try:
        for index in range(3):
            wf = Workflow(_parallel_sleep_wf(0.05), max_workers=4)
            events = [event async for event in wf.astream(
                {"x": "hi"}, run_context={"run_dir": str(tmp_path)},
            )]
            finished = next(event for event in events if event.get("status") == "finished")
            assert not finished.get("error_dict"), finished
            assert finished["final_outputs"]["__end__"] == {"a": "hi-A", "b": "hi-B"}
            assert {p.pid for p in _child_code_workers()} <= before
            assert len(spy_pool.instances) == index + 1
    finally:
        for pool in spy_pool.instances:
            pool.close()


@pytest.mark.asyncio
async def test_parallel_branches_run_concurrently(tmp_path):
    import json
    import os
    import sys

    sleep = 0.5
    wf = Workflow(_parallel_sleep_wf(sleep), max_workers=4)

    # Pre-warm the run's pool with 2 idle workers and inject it via run_context
    # (the engine's _get_run_pool honors a pre-supplied extra["_code_pool"]).
    # This isolates the concurrency contract from the host's highly
    # variable Python interpreter cold-start (a fresh worker spawn here can take
    # several seconds, which would otherwise serialize the two cold branches and
    # make the overlap assertion an interpreter-startup test, not a pool test).
    pool = code_runner.CodeWorkerPool(
        pythonpath=os.pathsep.join(sys.path), cwd=str(tmp_path), max_workers=4
    )
    warm = "def process_fn(inputs):\n    return {'ok': 1}"
    await asyncio.gather(
        asyncio.to_thread(pool.run, warm, {}, 30),
        asyncio.to_thread(pool.run, warm, {}, 30),
    )
    assert len(pool._idle) >= 2, "failed to pre-warm 2 idle workers"

    finished = None
    async for ev in wf.astream(
        {"x": "hi"},
        run_context={"run_id": "r", "run_dir": str(tmp_path), "_code_pool": pool},
    ):
        if ev.get("status") == "finished":
            finished = ev

    assert finished is not None and not finished.get("error_dict"), finished
    # Each branch got its OWN correct output (no interleave on a shared worker).
    end = finished["final_outputs"].get("__end__")
    assert end == {"a": "hi-A", "b": "hi-B"}, f"branch outputs wrong/interleaved: {end}"

    # Each branch recorded its sleep WINDOW [start, end] (shared wall clock,
    # comparable across the two worker subprocesses). Concurrency ⟺ the windows
    # OVERLAP — if the branches ran serially on a single worker, B would only
    # start after A finished (disjoint windows). This is immune to the
    # environment's seconds-long interpreter cold-start (which inflates a raw
    # end-to-end wall-clock threshold).
    a0, a1 = json.loads((tmp_path / "win_A.json").read_text())
    b0, b1 = json.loads((tmp_path / "win_B.json").read_text())
    overlap = min(a1, b1) - max(a0, b0)
    assert overlap > 0, (
        f"branch sleep windows did NOT overlap (A={a0:.3f}..{a1:.3f}, "
        f"B={b0:.3f}..{b1:.3f}) — branches ran serially, not concurrently on the pool"
    )
    # Sanity: the overlap is a real fraction of the sleep, not a 1ms fluke.
    assert overlap > sleep * 0.5, (
        f"branches barely overlapped ({overlap:.3f}s of {sleep}s) — not genuinely concurrent"
    )


# --------------------------------------------------------------------------- #
# 4. teardown at run end (success)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pool_torn_down_at_run_end(tmp_path):
    before = set(p.pid for p in _child_code_workers())
    wf = Workflow(_three_codenode_wf(), max_workers=4)
    finished = None
    async for ev in wf.astream({}, run_context={"run_id": "r", "run_dir": str(tmp_path)}):
        if ev.get("status") == "finished":
            finished = ev
    assert finished is not None and not finished.get("error_dict"), finished

    # Give the OS a brief moment to reap the killed workers.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        leaked = set(p.pid for p in _child_code_workers()) - before
        if not leaked:
            break
        time.sleep(0.05)
    leaked = set(p.pid for p in _child_code_workers()) - before
    assert not leaked, f"leaked code_worker subprocesses after run end: {leaked}"


# --------------------------------------------------------------------------- #
# 5. teardown on error
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pool_torn_down_on_error(tmp_path):
    before = set(p.pid for p in _child_code_workers())
    wf = Workflow(_error_then_code_wf(), max_workers=4)
    finished = None
    async for ev in wf.astream({}, run_context={"run_id": "r", "run_dir": str(tmp_path)}):
        if ev.get("status") == "finished":
            finished = ev
    # The first CodeNode errored — the run finishes with an error_dict entry.
    assert finished is not None
    assert finished.get("error_dict"), "expected the boom node to record an error"

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        leaked = set(p.pid for p in _child_code_workers()) - before
        if not leaked:
            break
        time.sleep(0.05)
    leaked = set(p.pid for p in _child_code_workers()) - before
    assert not leaked, f"leaked code_worker subprocesses after error run: {leaked}"


# --------------------------------------------------------------------------- #
# 6. cancel kills workers mid-run
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_kills_workers(tmp_path):
    before = set(p.pid for p in _child_code_workers())
    wf = Workflow(_long_code_wf(sleep=30.0), max_workers=4)
    stop_event = asyncio.Event()

    started = asyncio.Event()

    async def _drive():
        async for ev in wf.astream(
            {}, stop_event=stop_event, run_context={"run_id": "r", "run_dir": str(tmp_path)}
        ):
            # Once the slow CodeNode's 'running' frame fires, a worker is spawned
            # and mid-sleep — signal that we can cancel.
            if ev.get("status") == "running" and ev.get("node_id") == "node_2":
                started.set()

    task = asyncio.create_task(_drive())

    # Wait until the slow CodeNode is actually running (worker spawned + mid-sleep).
    await asyncio.wait_for(started.wait(), timeout=10)
    # Give to_thread a beat to actually enter pool.run() / spawn the worker.
    await asyncio.sleep(0.3)
    assert _child_code_workers(), "expected a live worker mid-run before cancel"

    # Cancel the run.
    stop_event.set()

    # The run must END promptly (well under the 30s sleep) — the pool teardown
    # SIGKILLs the in-flight worker, unblocking the to_thread call.
    await asyncio.wait_for(task, timeout=15)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        leaked = set(p.pid for p in _child_code_workers()) - before
        if not leaked:
            break
        time.sleep(0.05)
    leaked = set(p.pid for p in _child_code_workers()) - before
    assert not leaked, f"orphan code_worker subprocess survived cancel: {leaked}"
