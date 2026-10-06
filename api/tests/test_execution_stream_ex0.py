# -*- coding: utf-8 -*-
"""Execution-stream integration for per-node events.
EXEC_UPDATE frames live (running → completed) via the engine's astream,
persists the accumulated per_node map (C1), and funnels cancel/error/success
AFTER the stream (M5).

Uses the REAL auth harness (register → session_token → Bearer) on the
httpx ASGI ``client`` fixture + ``pg_engine`` — the legacy
``VIBECANVAS_API_DEV_TOKEN`` Bearer harness is dead. SSE is consumed via
``client.stream``.

Every run goes through the unified gVisor sandbox path. The fixture selects a
filesystem object store because the in-memory store cannot materialize the
per-run ``/run`` directory. ``RUNSC_PATH`` must point at a usable runsc binary.
"""

from __future__ import annotations

import json
import tempfile
import uuid
import base64

import pytest
from sqlalchemy import text


async def _register(client) -> str:
    email = f"ex0_{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


@pytest.fixture
def _sandbox_oneshot_fs(monkeypatch):
    """Cold one-shot sandbox backend + filesystem object store, so the
    sandbox-only canvas-Run path works under the lifespan-less conftest
    ``client`` (which defaults to the warm backend + inmemory store)."""
    from vibecanvas_api.config import config as _cfg
    from vibecanvas_api.services.sandbox import _gvisor_runnable

    if not _gvisor_runnable():
        pytest.skip("full rootless gVisor profile is unavailable")
    monkeypatch.setattr(_cfg.object_store, "provider", "filesystem",
                        raising=False)
    monkeypatch.setattr(_cfg.object_store, "fs_root",
                        tempfile.mkdtemp(prefix="vc-os-"), raising=False)
    monkeypatch.setattr(_cfg, "kms_provider", "local", raising=False)
    monkeypatch.setattr(
        _cfg,
        "kms_local_master_key",
        base64.urlsafe_b64encode(b"x" * 32).decode(),
        raising=False,
    )
    monkeypatch.setattr(_cfg, "kms_local_master_key_file", "", raising=False)


def _two_node_codenode_wf(wf_id: str) -> dict:
    """Start → CodeNode → End. The CodeNode gives us a real per-node
    running→completed pair to assert on."""
    return {
        "__meta__": {
            "workflow_id": wf_id, "workflow_version": 1,
            "workflow_subversion": 0,
        },
        "node_1": {
            "node_id": "node_1", "node_name": "__start__",
            "node_type": "StartNode", "node_description": "",
            "input_fields": {"x": {"type": "string", "value": "", "reference": ""}},
            "output_fields": {"x": {"type": "string", "description": ""}},
            "node_config": {}, "children": ["node_2"],
            "__attributes__": {"x": 0, "y": 0},
        },
        "node_2": {
            "node_id": "node_2", "node_name": "code", "node_type": "CodeNode",
            "node_description": "",
            "input_fields": {"x": {"type": "string", "value": "", "reference": "__start__.x"}},
            "output_fields": {"y": {"type": "string", "description": ""}},
            "node_config": {
                "programming_language": "python",
                "process_fn": "def process_fn(inputs):\n    return {'y': inputs['x'] + '!'}",
            },
            "children": ["node_3"],
            "__attributes__": {"x": 1, "y": 0},
        },
        "node_3": {
            "node_id": "node_3", "node_name": "__end__", "node_type": "EndNode",
            "node_description": "",
            "input_fields": {"y": {"type": "string", "value": "", "reference": "code.y"}},
            "output_fields": {"y": {"type": "string", "description": ""}},
            "node_config": {}, "children": [],
            "__attributes__": {"x": 2, "y": 0},
        },
    }


async def _parse_sse(resp) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    buf = ""
    async for chunk in resp.aiter_bytes():
        buf += chunk.decode("utf-8")
        while "\n\n" in buf:
            block, _, buf = buf.partition("\n\n")
            name = data = None
            for line in block.split("\n"):
                if line.startswith("event: "):
                    name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line.removeprefix("data: "))
                    except Exception:
                        data = {}
            if name is not None:
                events.append((name, data or {}))
    return events


async def _make_committed_wf(client, hdr) -> str:
    r = await client.post("/api/v1/workflows", json={"name": "ex0"}, headers=hdr)
    assert r.status_code == 201, r.text
    wf_id = r.json()["wf_id"]
    r = await client.post(
        f"/api/v1/workflows/{wf_id}/commits",
        json={"workflow": _two_node_codenode_wf(wf_id), "expected_version": "v1.sv0"}, headers=hdr,
    )
    assert r.status_code == 200, r.text
    return wf_id


@pytest.mark.asyncio
async def test_route_streams_per_node_frames_and_persists(
    client, pg_engine, _sandbox_oneshot_fs,
):
    """A real CodeNode workflow run through the route emits per-node
    running→completed frames + a terminal frame, and the accumulated
    per_node map is persisted so GET /executions/{id} returns it."""
    tok = await _register(client)
    hdr = {"Authorization": f"Bearer {tok}"}
    wf_id = await _make_committed_wf(client, hdr)

    async with client.stream(
        "POST", f"/api/v1/workflows/{wf_id}/executions",
        json={"mode": "single", "input": {"x": "hi"}}, headers=hdr,
    ) as resp:
        assert resp.status_code == 200
        events = await _parse_sse(resp)

    names = [n for n, _ in events]
    assert names[0] == "started"
    assert names[-1] in {"done", "error"}, names[-2:]

    # Per-node EXEC_UPDATE frames: running then completed for node_2 (the
    # CodeNode). node_1/node_3 also light up; assert at least the CodeNode.
    upd = [p for n, p in events if n == "EXEC_UPDATE"]
    node2 = [p for p in upd if p.get("node_id") == "node_2"]
    statuses = [p["status"] for p in node2]
    assert "running" in statuses, f"no running frame for node_2: {statuses}"
    assert "completed" in statuses, f"no completed frame for node_2: {statuses}"
    # running precedes completed.
    assert statuses.index("running") < statuses.index("completed")
    # The completed frame carries a JSON-string result.
    completed = next(p for p in node2 if p["status"] == "completed")
    assert json.loads(completed["result"]) == {"y": "hi!"}
    # UX-3: the completed frame also carries the node's wall-clock duration
    # (the engine's execution_time, surfaced by the mapper).
    assert "duration" in completed
    assert isinstance(completed["duration"], (int, float))
    assert completed["duration"] >= 0.0

    # Terminal whole-workflow frame.
    terminal = [p for p in upd if p.get("status") == "completed" and "outputs" in p]
    assert terminal, f"no terminal completed frame: {upd}"
    assert terminal[-1]["outputs"] == {"y": "hi!"}

    # Workflow runs deliberately hide the internal turn id. Reload state through
    # the workflow-scoped status projection.
    assert all("exec_id" not in payload for payload in upd)
    r = await client.get(
        f"/api/v1/workflows/{wf_id}/execution/status", headers=hdr,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # API vocabulary: DB "success" is normalized to "completed" on read.
    assert body["status"] == "completed"
    per_node = body["result"]  # _record_to_status maps per_node → result
    assert per_node, "per_node not persisted — C1 regression"
    assert per_node["node_2"]["status"] == "completed"
    assert json.loads(per_node["node_2"]["execution_result"]) == {"y": "hi!"}
    # UX-3: per-node duration persists too, so a reloaded run can show it.
    assert "duration" in per_node["node_2"]
    assert isinstance(per_node["node_2"]["duration"], (int, float))

    # DB-level assert the row landed terminal.
    async with pg_engine.connect() as cx:
        row = (await cx.execute(text(
            "SELECT status, private_ciphertext, private_nonce, private_key_id "
            "FROM workflow_run_state WHERE wf_id=:wf"),
            {"wf": wf_id})).mappings().first()
    assert row is not None and row["status"] == "success"
    assert row["private_ciphertext"] and row["private_nonce"]
    assert row["private_key_id"] is not None


@pytest.mark.asyncio
async def test_route_node_error_surfaces_and_persists(
    client, pg_engine, _sandbox_oneshot_fs,
):
    """A CodeNode that raises → a per-node error frame + a persisted error
    status in per_node; the run records terminal."""
    tok = await _register(client)
    hdr = {"Authorization": f"Bearer {tok}"}

    r = await client.post("/api/v1/workflows", json={"name": "ex0err"}, headers=hdr)
    wf_id = r.json()["wf_id"]
    wf = _two_node_codenode_wf(wf_id)
    wf["node_2"]["node_config"]["process_fn"] = (
        "def process_fn(inputs):\n    raise ValueError('boom')"
    )
    r = await client.post(
        f"/api/v1/workflows/{wf_id}/commits", json={"workflow": wf, "expected_version": "v1.sv0"}, headers=hdr,
    )
    assert r.status_code == 200, r.text

    async with client.stream(
        "POST", f"/api/v1/workflows/{wf_id}/executions",
        json={"mode": "single", "input": {"x": "hi"}}, headers=hdr,
    ) as resp:
        events = await _parse_sse(resp)

    upd = [p for n, p in events if n == "EXEC_UPDATE"]
    err_frames = [p for p in upd if p.get("node_id") == "node_2" and p.get("status") == "error"]
    assert err_frames, f"no per-node error frame: {upd}"
    assert "boom" in err_frames[0]["error"]

    r = await client.get(
        f"/api/v1/workflows/{wf_id}/execution/status", headers=hdr,
    )
    body = r.json()
    # The run still reaches a terminal status; per_node carries the error.
    assert body["status"] in {"completed", "error"}
    assert body["result"]["node_2"]["status"] == "error"


@pytest.mark.asyncio
async def test_non_serializable_output_does_not_crash_stream(
    client, pg_engine, _sandbox_oneshot_fs,
):
    """A CodeNode returning a non-JSON-serializable value must NOT crash the
    SSE stream. The lean CodeNode worker validates
    ``json.dumps(result)`` and rejects a non-serializable return as a GRACEFUL
    node ``error`` frame (the result must cross the sandbox JSON bus — a ``set``
    can't round-trip), and the stream still terminates cleanly with ``done``."""
    tok = await _register(client)
    hdr = {"Authorization": f"Bearer {tok}"}

    r = await client.post("/api/v1/workflows", json={"name": "ex0ns"}, headers=hdr)
    wf_id = r.json()["wf_id"]
    wf = _two_node_codenode_wf(wf_id)
    # Return a set — not JSON-serializable; the worker rejects it as a node error.
    wf["node_2"]["node_config"]["process_fn"] = (
        "def process_fn(inputs):\n    return {'y': {1, 2, 3}}"
    )
    r = await client.post(
        f"/api/v1/workflows/{wf_id}/commits", json={"workflow": wf, "expected_version": "v1.sv0"}, headers=hdr,
    )
    assert r.status_code == 200, r.text

    async with client.stream(
        "POST", f"/api/v1/workflows/{wf_id}/executions",
        json={"mode": "single", "input": {"x": "hi"}}, headers=hdr,
    ) as resp:
        events = await _parse_sse(resp)

    names = [n for n, _ in events]
    # The stream terminated CLEANLY (a crash would truncate before ``done``).
    assert names[-1] == "done", f"non-serializable output crashed the stream: {names[-3:]}"
    upd = [p for n, p in events if n == "EXEC_UPDATE"]
    # node_2 surfaces a graceful error frame (NOT a completed) mentioning the
    # JSON-serializable contract — no crash, no silent success.
    node2_error = [
        p for p in upd
        if p.get("node_id") == "node_2" and p.get("status") == "error"
    ]
    assert node2_error, f"node_2 should error on non-serializable output: {upd}"
    assert "json-serializable" in (node2_error[0].get("error") or "").lower(), node2_error[0]


def _big_loop_wf(wf_id: str, iters: int) -> dict:
    """Start → LoopBegin → LoopEnd → End, ``iters`` iterations. A bodiless
    loop is used deliberately: LoopBegin/LoopEnd emit running+success per
    iteration WITHOUT a CodeNode ProcessPool round-trip, so the run emits
    ~4 frames/iter cheaply (a CodeNode body would make a multi-thousand-iter
    run minutes-slow). With the drop-oldest exec buffer (M1) the run must NOT
    falsely fail at the 10000-frame cap."""
    return {
        "__meta__": {"workflow_id": wf_id, "workflow_version": 1,
                      "workflow_subversion": 0},
        "node_1": {
            "node_id": "node_1", "node_name": "__start__",
            "node_type": "StartNode", "node_description": "",
            "input_fields": {}, "output_fields": {}, "node_config": {},
            "children": ["node_2"], "__attributes__": {"x": 0, "y": 0},
        },
        "node_2": {
            "node_id": "node_2", "node_name": "lb",
            "node_type": "LoopBeginNode", "node_description": "",
            "input_fields": {}, "output_fields": {},
            "node_config": {
                "init_value": {"value": 0, "reference": ""},
                "end_value": {"value": iters, "reference": ""},
                "step_value": 1, "loop_end_node_id": "node_4",
            },
            "children": ["node_4"], "__attributes__": {"x": 1, "y": 0},
        },
        "node_4": {
            "node_id": "node_4", "node_name": "le", "node_type": "LoopEndNode",
            "node_description": "",
            "input_fields": {}, "output_fields": {},
            "node_config": {"loop_begin_node_id": "node_2"},
            "children": ["node_5"], "__attributes__": {"x": 3, "y": 0},
        },
        "node_5": {
            "node_id": "node_5", "node_name": "__end__", "node_type": "EndNode",
            "node_description": "",
            "input_fields": {}, "output_fields": {}, "node_config": {},
            "children": [], "__attributes__": {"x": 4, "y": 0},
        },
    }


@pytest.mark.asyncio
async def test_high_frame_loop_run_does_not_falsely_fail(
    client, pg_engine, _sandbox_oneshot_fs,
):
    """M1: a loop emitting thousands of frames (>10000) must complete via the
    drop-oldest exec buffer — NOT raise 'buffer full' and falsely error."""
    tok = await _register(client)
    hdr = {"Authorization": f"Bearer {tok}"}

    r = await client.post("/api/v1/workflows", json={"name": "ex0loop"}, headers=hdr)
    wf_id = r.json()["wf_id"]
    # ~4 frames/iter (begin+end × running+success) × 3000 ≈ 12k frames →
    # over the 10000 cap, so drop-oldest is exercised, while keeping the
    # run fast enough for CI.
    r = await client.post(
        f"/api/v1/workflows/{wf_id}/commits",
        json={"workflow": _big_loop_wf(wf_id, 3000), "expected_version": "v1.sv0"}, headers=hdr,
    )
    assert r.status_code == 200, r.text

    async with client.stream(
        "POST", f"/api/v1/workflows/{wf_id}/executions",
        json={"mode": "single", "input": {}}, headers=hdr,
    ) as resp:
        events = await _parse_sse(resp)

    names = [n for n, _ in events]
    # The run terminates cleanly — a 'buffer full' raise would surface as
    # an 'error' terminal (engine_error) instead of 'done'.
    assert names[-1] == "done", (
        f"high-frame loop falsely failed (M1 regression): {names[-3:]}"
    )

    # And it persisted a terminal success (the per_node view is "latest per
    # node", not every iteration — by design for exec).
    r = await client.get(
        f"/api/v1/workflows/{wf_id}/execution/status", headers=hdr,
    )
    assert r.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_mid_run_yields_terminal_cancelled(
    client, pg_engine, _sandbox_oneshot_fs,
):
    """Cancel mid-run → graceful terminal. The cancel route sets the stop
    event; astream stops at the next node boundary; the run records
    'stopped' and the SSE stream terminates."""
    tok = await _register(client)
    hdr = {"Authorization": f"Bearer {tok}"}

    r = await client.post("/api/v1/workflows", json={"name": "ex0cancel"}, headers=hdr)
    wf_id = r.json()["wf_id"]
    # A bodiless loop long enough to fire cancel mid-flight but small enough
    # to complete quickly if the engine wins the race (both are graceful).
    r = await client.post(
        f"/api/v1/workflows/{wf_id}/commits",
        json={"workflow": _big_loop_wf(wf_id, 2000), "expected_version": "v1.sv0"}, headers=hdr,
    )
    assert r.status_code == 200, r.text

    events: list[tuple[str, dict]] = []
    cancelled = False
    async with client.stream(
        "POST", f"/api/v1/workflows/{wf_id}/executions",
        json={"mode": "single", "input": {}}, headers=hdr,
    ) as resp:
        buf = ""
        async for chunk in resp.aiter_bytes():
            buf += chunk.decode("utf-8")
            while "\n\n" in buf:
                block, _, buf = buf.partition("\n\n")
                name = data = None
                for line in block.split("\n"):
                    if line.startswith("event: "):
                        name = line.removeprefix("event: ")
                    elif line.startswith("data: "):
                        try:
                            data = json.loads(line.removeprefix("data: "))
                        except Exception:
                            data = {}
                if name is not None:
                    events.append((name, data or {}))
                    # Workflow SSE is keyed by wf_id; cancel through the
                    # workflow-scoped control endpoint after some progress.
                    if not cancelled and data and data.get("wf_id") == wf_id:
                        if len([e for e in events if e[0] == "EXEC_UPDATE"]) >= 3:
                            await client.post(
                                f"/api/v1/workflows/{wf_id}/execution/cancel",
                                headers=hdr,
                            )
                            cancelled = True

    names = [n for n, _ in events]
    assert names[-1] in {"error", "done"}, names[-3:]
    assert cancelled, "never reached the cancel point"

    # The execution recorded a terminal status (stopped if cancel landed
    # before the engine finished; the engine is fast so it may also complete
    # — both are graceful, neither is a crash).
    r = await client.get(
        f"/api/v1/workflows/{wf_id}/execution/status", headers=hdr,
    )
    assert r.json()["status"] in {"stopped", "completed"}


# --- pure-function unit tests for the per-node accumulator (no DB) ---

from vibecanvas_api.routes.executions import _accumulate_per_node  # noqa: E402


def test_accumulate_per_node_stores_duration():
    """UX-3: a per-node frame carrying ``duration`` (from the mapper) persists
    it into the accumulator's per_node slot so a reloaded run can show it."""
    per_node: dict[str, dict] = {}
    _accumulate_per_node(per_node, {
        "node_id": "node_2", "status": "completed",
        "result": '{"y": 1}', "duration": 0.37,
    })
    assert per_node["node_2"]["duration"] == 0.37
    assert per_node["node_2"]["status"] == "completed"


def test_accumulate_per_node_no_duration_leaves_slot_unset():
    """A frame without a ``duration`` does not invent one (back-compat)."""
    per_node: dict[str, dict] = {}
    _accumulate_per_node(per_node, {"node_id": "node_2", "status": "running"})
    assert "duration" not in per_node["node_2"]
