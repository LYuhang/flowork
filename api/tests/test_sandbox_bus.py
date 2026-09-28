# -*- coding: utf-8 -*-
"""API-side host bus broker and execution-route sandbox branch.

Layers:

  * Unit (no gVisor): ``socket_path_for`` ≤107 assertion (incl. a long configured
    fs_root path → assert raises); the broker reads framed messages from a
    hand-driven UDS client (the engine framing); the route flag-OFF keeps the
    in-process path (regression); the route flag-ON + sandbox-runnable takes the
    sandbox branch (provider + broker mocked).
  * Guarded gVisor (skipif not ``_gvisor_runnable()``): a REAL one-shot sandbox
    run with ``--host-uds=open`` + the bus → the host receives per-node
    ``node_event``s live + a terminal ``result``; cancel → killpg + run_dir
    retained.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibecanvas_api.services.sandbox import _gvisor_runnable, get_sandbox_provider
from vibecanvas_api.services.sandbox.bus_broker import (
    BUS_ROOT,
    MAX_SOCKET_PATH,
    BusBroker,
    socket_path_for,
)
from vibecanvas_engine.sandbox_bus import encode_frame


# --------------------------------------------------------------------------- #
# socket_path_for ≤107 assertion
# --------------------------------------------------------------------------- #
def test_socket_path_for_short_and_under_limit():
    p = socket_path_for("e_deadbeefcafebabe")
    assert p.startswith(BUS_ROOT + "/")
    assert p.endswith("/bus.sock")
    assert len(p.encode("utf-8")) <= MAX_SOCKET_PATH


def test_socket_path_for_per_run_distinct():
    a = socket_path_for("aaaaaaaa1111")
    b = socket_path_for("bbbbbbbb2222")
    # PER-RUN dir (FIX-4 cross-run-leak): distinct dirs, not a shared one.
    assert os.path.dirname(a) != os.path.dirname(b)


def test_socket_path_for_distinguishes_namespaced_ids_with_the_same_prefix():
    alpha = socket_path_for("background-job_alpha")
    beta = socket_path_for("background-job_beta")
    assert os.path.dirname(alpha) != os.path.dirname(beta)


def test_socket_path_for_asserts_over_limit(monkeypatch):
    """A pathological BUS_ROOT (a long configured fs_root) blows the AF_UNIX 107
    limit → ``socket_path_for`` ASSERTS rather than silently failing ``bind``."""
    import vibecanvas_api.services.sandbox.bus_broker as bb
    long_root = "/" + ("x" * 120)
    monkeypatch.setattr(bb, "BUS_ROOT", long_root)
    with pytest.raises(AssertionError):
        bb.socket_path_for("e_deadbeef")


# --------------------------------------------------------------------------- #
# BusBroker reads framed messages from a hand-driven UDS (no gVisor)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_broker_reads_framed_messages():
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "vcbus", "run8", "bus.sock")
        broker = BusBroker(sock)
        await broker.start()
        assert os.path.exists(sock)  # pathname socket bound.

        async def _client():
            reader, writer = await asyncio.open_unix_connection(sock)
            writer.write(encode_frame({"type": "node_event", "status": "running",
                                       "node_id": "n1"}))
            writer.write(encode_frame({"type": "result", "final_outputs": {"ok": 1},
                                       "error_dict": {}, "execution_time": 0.2}))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        client_task = asyncio.create_task(_client())
        got = []
        async for msg in broker.messages():
            got.append(msg)
        await client_task
        await broker.close()

        assert got[0]["type"] == "node_event" and got[0]["node_id"] == "n1"
        assert got[1]["type"] == "result" and got[1]["final_outputs"] == {"ok": 1}
        # close() unlinked the socket + rmtree'd the per-run dir.
        assert not os.path.exists(sock)


@pytest.mark.asyncio
async def test_broker_close_idempotent():
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "run8", "bus.sock")
        broker = BusBroker(sock)
        await broker.start()
        await broker.close()
        await broker.close()  # idempotent — no raise.


@pytest.mark.asyncio
async def test_broker_health_rejects_peer_eof_before_next_send():
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "health", "bus.sock")
        broker = BusBroker(sock)
        await broker.start()
        _reader, writer = await asyncio.open_unix_connection(sock)
        await broker.wait_connected()
        assert broker.is_connected() is True

        writer.close()
        await writer.wait_closed()
        for _ in range(50):
            if not broker.is_connected():
                break
            await asyncio.sleep(0.01)

        assert broker.is_connected() is False
        with pytest.raises(ConnectionError, match="not writable"):
            await broker.send({"type": "runtime_request"})
        await broker.close()


# --------------------------------------------------------------------------- #
# Route branch — flag OFF (regression) / flag ON (sandbox branch)
# --------------------------------------------------------------------------- #
def _build_minimal_wf() -> dict:
    return {
        "__meta__": {"workflow_id": "wf1", "workflow_version": 1,
                     "workflow_subversion": 0},
        "node_1": {
            "node_id": "node_1", "node_name": "__start__",
            "node_type": "StartNode", "node_description": "",
            "input_fields": {}, "output_fields": {}, "node_config": {},
            "children": [],
        },
    }


def _patch_persistence(monkeypatch):
    """Stub the executions-route DB persistence so the producer can run without
    a real Postgres session (the branch decision is what's under test)."""
    import vibecanvas_api.routes.executions as ex

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(ex, "session_scope", lambda **kw: _FakeSession())

    fake_repo = MagicMock()
    fake_repo.start_execution = AsyncMock()
    fake_repo.finish_execution = AsyncMock()
    fake_repo.stop_execution = AsyncMock()
    monkeypatch.setattr(ex, "ExecutionRepo", lambda *a, **k: fake_repo)
    monkeypatch.setattr(ex.stop_registry, "discard", lambda *a, **k: None)
    return fake_repo


async def _drain(gen):
    out = []
    async for ev in gen:
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_route_sandbox_runnable_always_uses_sandbox(monkeypatch):
    """P2 sandbox-only (#629): the in-process ``astream`` fallback was REMOVED.
    A sandbox-runnable workflow ALWAYS delegates to
    ``_produce_execution_sandbox`` — there is no longer a flag gate or an
    in-process path (constructing ``Workflow`` would be a bug)."""
    import vibecanvas_api.routes.executions as ex
    _patch_persistence(monkeypatch)

    sandbox_called = {"n": 0}
    async def _fake_sandbox(*a, **k):
        sandbox_called["n"] += 1
        yield ("EXEC_UPDATE", {"status": "completed", "from": "sandbox"})
    monkeypatch.setattr(ex, "_produce_execution_sandbox", _fake_sandbox)

    # The in-process path is gone — if it were taken, Workflow() would run.
    class _BoomWF:
        def __init__(self, *a, **k):
            raise AssertionError("in-process path was removed (P2 sandbox-only)")
    import vibecanvas_engine
    monkeypatch.setattr(vibecanvas_engine, "Workflow", _BoomWF)

    body = SimpleNamespace(mode="single", input={})
    events = await _drain(ex._produce_execution(
        asyncio.Event(), "wf1", "e_x", body, _build_minimal_wf(), "u1", "t1"))

    assert sandbox_called["n"] == 1
    assert any(p.get("from") == "sandbox" for _, p in events)


@pytest.mark.asyncio
async def test_route_flag_on_takes_sandbox_branch(monkeypatch):
    """Flag ON + sandbox-runnable → the sandbox branch runs (the in-process
    astream is NOT used). ``_produce_execution_sandbox`` is the delegate."""
    import vibecanvas_api.routes.executions as ex
    _patch_persistence(monkeypatch)
    from vibecanvas_api.config import config as _cfg
    monkeypatch.setattr(_cfg, "sandbox_debug_execute_enabled", True,
                        raising=False)

    sandbox_called = {"n": 0}
    async def _fake_sandbox(stop, wf_id, exec_id, body, wf_dict, uid, tid):
        sandbox_called["n"] += 1
        yield ("EXEC_UPDATE", {"exec_id": exec_id, "status": "completed",
                               "from": "sandbox"})
    monkeypatch.setattr(ex, "_produce_execution_sandbox", _fake_sandbox)

    # If the in-process path were wrongly taken, Workflow() would be constructed.
    class _BoomWF:
        def __init__(self, *a, **k):
            raise AssertionError("in-process path must not run when flag on")
    import vibecanvas_engine
    monkeypatch.setattr(vibecanvas_engine, "Workflow", _BoomWF)

    body = SimpleNamespace(mode="single", input={})
    events = await _drain(ex._produce_execution(
        asyncio.Event(), "wf1", "e_y", body, _build_minimal_wf(), "u1", "t1"))

    assert sandbox_called["n"] == 1
    assert any(p.get("from") == "sandbox" for _, p in events)


@pytest.mark.asyncio
async def test_route_non_runnable_yields_terminal_error(monkeypatch):
    """P2 sandbox-only (#629): a workflow the sandbox cannot run
    (``classify_workflow`` raises ``EngineNeedsHostNode``) does not fall back
    to an in-process run — it
    yields a CLEAR terminal ``error`` frame (persisted) instead of silently
    degrading to an unsandboxed host run. The sandbox delegate is never
    reached for a non-runnable workflow."""
    import vibecanvas_api.routes.executions as ex
    _patch_persistence(monkeypatch)

    sandbox_called = {"n": 0}
    async def _fake_sandbox(*a, **k):
        sandbox_called["n"] += 1
        yield ("EXEC_UPDATE", {"status": "from-sandbox"})
    monkeypatch.setattr(ex, "_produce_execution_sandbox", _fake_sandbox)

    # classify_workflow raises → not sandbox-runnable.
    from vibecanvas_api.services.sandbox import EngineNeedsHostNode
    monkeypatch.setattr(
        ex,
        "classify_workflow",
        MagicMock(side_effect=EngineNeedsHostNode("HostNode")),
    )

    # The in-process path is gone — Workflow() must never be constructed.
    class _BoomWF:
        def __init__(self, *a, **k):
            raise AssertionError("in-process fallback was removed (P2 sandbox-only)")
    import vibecanvas_engine
    monkeypatch.setattr(vibecanvas_engine, "Workflow", _BoomWF)

    body = SimpleNamespace(mode="single", input={})
    events = await _drain(ex._produce_execution(
        asyncio.Event(), "wf1", "e_z", body, _build_minimal_wf(), "u1", "t1"))

    # Non-runnable → no sandbox delegation, a terminal error frame instead.
    assert sandbox_called["n"] == 0
    assert any(p.get("status") == "error" for _, p in events)
    assert any("cannot run in the sandbox" in (p.get("error") or "")
               for _, p in events)


# --------------------------------------------------------------------------- #
# Guarded gVisor — the headline: a REAL one-shot sandbox run over the bus.
# --------------------------------------------------------------------------- #
def _kernel_wf() -> dict:
    meta = {"workflow_id": "wf_bus", "workflow_version": 1,
            "workflow_subversion": 0}
    code = (
        "def process_fn(inputs):\n"
        "    return {'v': inputs['x'] + 1}\n"
    )
    return {
        "__meta__": meta,
        "node_1": {
            "node_id": "node_1", "node_name": "__start__",
            "node_type": "StartNode", "node_description": "",
            "input_fields": {},
            "output_fields": {"x": {"type": "integer", "description": "n"}},
            "node_config": {}, "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2", "node_name": "compute",
            "node_type": "CodeNode", "node_description": "inc",
            "input_fields": {"x": {"type": "integer", "value": 0,
                                   "reference": "__start__.x"}},
            "output_fields": {"v": {"type": "integer", "description": "x+1"}},
            "node_config": {"programming_language": "python",
                            "process_fn": code},
            "children": ["node_3"],
        },
        "node_3": {
            "node_id": "node_3", "node_name": "__end__",
            "node_type": "EndNode", "node_description": "",
            "input_fields": {"v": {"type": "integer", "value": 0,
                                   "reference": "compute.v"}},
            "output_fields": {"v": {"type": "integer", "description": "x+1"}},
            "node_config": {}, "children": [],
        },
    }


@pytest.mark.skipif(not _gvisor_runnable(),
                    reason="rootless gVisor not runnable here")
@pytest.mark.asyncio
async def test_gvisor_bus_live_node_events_and_result(tmp_path):
    """REAL one-shot sandbox run with --host-uds=open + the bus: the host
    receives per-node ``node_event``s live over the UDS AND a terminal
    ``result`` frame."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    provider = get_sandbox_provider()
    sock = socket_path_for("buslive1")
    broker = BusBroker(sock)
    await broker.start()
    handle = await asyncio.to_thread(
        provider.launch_workflow_bus,
        run_dir=str(run_dir), workflow=_kernel_wf(), inputs={"x": 41},
        run_id="buslive1", bus_socket=sock,
    )
    node_events = []
    result = None
    try:
        async for msg in broker.messages():
            if msg["type"] == "node_event":
                node_events.append(msg)
            elif msg["type"] == "result":
                result = msg
                break
    finally:
        await asyncio.to_thread(provider.stop_run, handle, kill=(result is None))
        await broker.close()

    assert result is not None, "no terminal result frame received over the bus"
    assert node_events, "no live node_event frames received over the bus"
    # The CodeNode ran inside the sandbox: x=41 → v=42 in the final outputs.
    fo = result["final_outputs"]
    assert fo.get("__end__", {}).get("v") == 42, fo


@pytest.mark.skipif(not _gvisor_runnable(),
                    reason="rootless gVisor not runnable here")
@pytest.mark.asyncio
async def test_gvisor_bus_cancel_killpg_retains_run_dir(tmp_path):
    """Cancel a live sandbox run → killpg the runsc group + the run_dir is
    RETAINED (debug state survives, FIX-5d). The run_dir's __exec__ inputs/
    workflow files remain after stop_run(kill=True)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    provider = get_sandbox_provider()
    sock = socket_path_for("buscanc1")
    broker = BusBroker(sock)
    await broker.start()
    handle = await asyncio.to_thread(
        provider.launch_workflow_bus,
        run_dir=str(run_dir), workflow=_kernel_wf(), inputs={"x": 1},
        run_id="buscanc1", bus_socket=sock,
    )
    # Cancel immediately (killpg) without consuming to completion.
    await asyncio.to_thread(provider.stop_run, handle, kill=True)
    await broker.close()

    # run_dir RETAINED — the host wrote workflow.json/inputs.json before launch.
    assert (run_dir / "__exec__" / "workflow.json").exists()
    assert (run_dir / "__exec__" / "inputs.json").exists()
    # The runsc process group is dead (killed).
    handle.proc.wait(timeout=10)
    assert handle.proc.returncode is not None


@pytest.fixture(autouse=True)
def select_gvisor_backend(monkeypatch):
    """These guarded tests exercise the explicitly selected gVisor backend."""
    from vibecanvas_api.config import config
    monkeypatch.setattr(config, "sandbox_runtime", "gvisor")
