"""RE-6 P2 T1 — pure-engine pieces for engine-in-sandbox.

1. ``ENGINE_PURE_NODE_TYPES`` — a frozen snapshot of the pure engine node
   types, captured at import time of ``vibecanvas_engine.nodes`` (before any
   api node can pollute the live ``node_registry``). The host uses it to tell
   pure-vs-api workflows apart.
2. ``sandbox_entry.run_exec(run_root, run_id)`` — the in-sandbox entrypoint
   (PURE, no api import) that reads ``{run_root}/__exec__/workflow.json`` +
   ``inputs.json``, runs ``astream``, and writes ``result.json`` +
   ``events.ndjson`` back into ``{run_root}/__exec__/``.
"""

from __future__ import annotations

import json
import time

from vibecanvas_engine.nodes import ENGINE_PURE_NODE_TYPES
from vibecanvas_engine import sandbox_entry


def _min_code_wf() -> dict:
    """Minimal Start -> Code -> End workflow.

    The CodeNode returns ``{"v": inputs["x"] + 1}``; End references it so the
    final_outputs carry the computed value.
    """
    return {
        "__meta__": {
            "workflow_id": "wf_sandbox_entry",
            "workflow_name": "sandbox_entry_smoke",
            "workflow_version": 1,
            "workflow_subversion": 0,
        },
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "StartNode",
            "node_description": "start",
            "input_fields": {},
            "output_fields": {
                "x": {"type": "integer", "description": "input number"},
            },
            "node_config": {},
            "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2",
            "node_name": "compute",
            "node_type": "CodeNode",
            "node_description": "increment x",
            "input_fields": {
                "x": {"type": "integer", "value": 0, "reference": "__start__.x"},
            },
            "output_fields": {
                "v": {"type": "integer", "description": "x + 1"},
            },
            "node_config": {
                "programming_language": "python",
                "process_fn": "def process_fn(inputs):\n    return {'v': inputs['x'] + 1}",
            },
            "children": ["node_3"],
        },
        "node_3": {
            "node_id": "node_3",
            "node_name": "__end__",
            "node_type": "EndNode",
            "node_description": "end",
            "input_fields": {
                "v": {"type": "integer", "value": 0, "reference": "compute.v"},
            },
            "output_fields": {
                "v": {"type": "integer", "description": "x + 1"},
            },
            "node_config": {},
            "children": [],
        },
    }


def test_pure_node_types_snapshot():
    assert "StartNode" in ENGINE_PURE_NODE_TYPES
    assert "CodeNode" in ENGINE_PURE_NODE_TYPES
    # api-defined node — never in the frozen engine-pure set.
    assert "KnowledgeSearchNode" not in ENGINE_PURE_NODE_TYPES
    assert isinstance(ENGINE_PURE_NODE_TYPES, frozenset)


def test_sandbox_entry_runs_and_writes_result(tmp_path):
    run = tmp_path
    exec_dir = run / "__exec__"
    exec_dir.mkdir()

    wf = _min_code_wf()
    (exec_dir / "workflow.json").write_text(json.dumps(wf))
    (exec_dir / "inputs.json").write_text(json.dumps({"x": 1}))

    rc = sandbox_entry.run_exec(str(run), run_id="r1")

    assert rc == 0

    res = json.loads((exec_dir / "result.json").read_text())
    assert res["error_dict"] == {}, f"engine reported errors: {res['error_dict']}"
    assert res["final_outputs"], "final_outputs is empty"
    # The End scope carries the computed value (x + 1 = 2).
    assert res["final_outputs"].get("__end__", {}).get("v") == 2
    assert "execution_time" in res

    # events.ndjson exists and every line is valid JSON.
    events_path = exec_dir / "events.ndjson"
    assert events_path.exists()
    lines = [ln for ln in events_path.read_text().splitlines() if ln.strip()]
    assert lines, "events.ndjson is empty"
    for ln in lines:
        json.loads(ln)


def test_invalid_row_input_is_a_result_not_a_worker_crash(tmp_path):
    exec_dir = tmp_path / "__exec__"
    exec_dir.mkdir()
    workflow = _min_code_wf()
    workflow["node_1"]["input_fields"] = {"x": {"type": "number", "value": 0}}
    (exec_dir / "workflow.json").write_text(json.dumps(workflow))
    (exec_dir / "inputs.json").write_text(json.dumps({"x": None}))

    assert sandbox_entry.run_exec(str(tmp_path), run_id="invalid-row") == 0
    result = json.loads((exec_dir / "result.json").read_text())
    assert result["final_outputs"] == {}
    assert "x:" in result["error_dict"]["__input__"]
    assert "__engine__" not in result["error_dict"]

    # The same resident worker can execute a subsequent valid row.
    (exec_dir / "inputs.json").write_text(json.dumps({"x": -3}))
    assert sandbox_entry.run_exec(str(tmp_path), run_id="next-row") == 0
    result = json.loads((exec_dir / "result.json").read_text())
    assert result["error_dict"] == {}
    assert result["final_outputs"]["__end__"]["v"] == -2


def test_invalid_single_node_input_is_a_result_not_a_worker_crash(tmp_path):
    exec_dir = tmp_path / "__exec__"
    exec_dir.mkdir()
    (exec_dir / "job.json").write_text(json.dumps({
        "node": {"node_id": "node_2", "node_type": "CodeNode",
                 "input_fields": {"x": {"type": "number"}}},
        "inputs": {"x": None},
    }))
    assert sandbox_entry.run_node_exec(str(tmp_path), run_id="invalid-node") == 0
    result = json.loads((exec_dir / "result.json").read_text())
    assert result["final_outputs"] == {}
    assert "x:" in result["error_dict"]["__input__"]


def test_run_exec_run_context_is_two_key(tmp_path, monkeypatch):
    """Identity-bind model: run_exec builds a 2-key run_context {run_id, run_dir}.
    Persistent files are mounted independently at ``/mount``, so there is no
    storage-directory key or path-resolve step in run_context."""
    run = tmp_path
    exec_dir = run / "__exec__"
    exec_dir.mkdir()
    (exec_dir / "workflow.json").write_text(json.dumps(_min_code_wf()))
    (exec_dir / "inputs.json").write_text(json.dumps({"x": 1}))

    captured: dict = {}

    async def _fake_drive(wf, inputs, run_context, events_path, **kwargs):
        captured["run_context"] = dict(run_context)
        with open(events_path, "w") as f:
            f.write("")
        return {}, {}, 0.0

    monkeypatch.setattr(sandbox_entry, "_drive", _fake_drive)

    rc = sandbox_entry.run_exec(str(run), run_id="r1")
    assert rc == 0
    assert "data_dir" not in captured["run_context"]
    assert set(captured["run_context"]) == {"run_id", "run_dir"}


def test_sandbox_entry_engine_crash_writes_error(tmp_path):
    """A malformed workflow (unknown node_type) → engine-level crash on build →
    result.json with error_dict={'__engine__': ...} and a nonzero return code."""
    run = tmp_path
    exec_dir = run / "__exec__"
    exec_dir.mkdir()

    bad_wf = {
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "TotallyUnknownNode",
            "input_fields": {},
            "output_fields": {},
            "node_config": {},
            "children": [],
        },
    }
    (exec_dir / "workflow.json").write_text(json.dumps(bad_wf))
    (exec_dir / "inputs.json").write_text(json.dumps({}))

    rc = sandbox_entry.run_exec(str(run), run_id="r1")

    assert rc == 1
    res = json.loads((exec_dir / "result.json").read_text())
    assert "__engine__" in res["error_dict"]
    assert res["final_outputs"] == {}


# ---------------------------------------------------------------------------
# RE-6 (warm cancel) — _drive watches the cancel marker → graceful stop
# ---------------------------------------------------------------------------
class _FakeWorkflow:
    """A Workflow stand-in whose ``astream`` emits N node events, checking the
    ``stop_event`` at each node boundary (mirrors the real producer's cooperative
    stop). Lets the cancel-marker watcher be exercised with NO gVisor / no real
    engine run. Records how many nodes actually ran."""

    def __init__(self, n_nodes=4, per_node_delay=0.08):
        self.n_nodes = n_nodes
        self.per_node_delay = per_node_delay
        self.ran = 0

    async def astream(self, inputs, stop_event=None, run_context=None):
        import asyncio as _aio
        finished_outputs = {}
        for i in range(1, self.n_nodes + 1):
            # Node boundary: the real producer stops here if cancel was requested.
            if stop_event is not None and stop_event.is_set():
                break
            self.ran = i
            nid = f"node_{i}"
            finished_outputs[nid] = {"v": i}
            yield {"status": "success", "node_id": nid, "execution_result": i}
            # Simulate node work so the watcher (50ms poll) can observe a marker
            # written by the test mid-run BEFORE the next node boundary.
            await _aio.sleep(self.per_node_delay)
        yield {
            "status": "finished",
            "final_outputs": finished_outputs,
            "error_dict": {},
        }


def test_drive_cancel_marker_stops_at_node_boundary(tmp_path):
    """A cancel marker dropped mid-run → astream stops at the NEXT node boundary
    (graceful), a partial result is returned, no hang."""
    import asyncio
    import threading

    exec_dir = tmp_path / "__exec__"
    exec_dir.mkdir()
    events_path = str(exec_dir / "events.ndjson")
    cancel_path = str(exec_dir / "cancel")

    wf = _FakeWorkflow(n_nodes=6, per_node_delay=0.08)

    def _drop_marker():
        # Drop the marker shortly after the run starts — well before all 6 nodes
        # would finish (6 * 0.08 = 0.48s) so the run is cut short.
        time.sleep(0.12)
        open(cancel_path, "w").close()

    t = threading.Thread(target=_drop_marker, daemon=True)
    t.start()
    final_outputs, error_dict, exec_time = asyncio.run(
        sandbox_entry._drive(
            wf, {}, {"run_id": "r1", "run_dir": str(tmp_path)},
            events_path, cancel_path=cancel_path,
        )
    )
    t.join(timeout=2.0)

    # Stopped EARLY (not all 6 nodes ran) but ran at least one node → partial.
    assert 0 < wf.ran < 6, f"expected a partial run, ran={wf.ran}"
    # A partial result is still returned (finished event emitted post-break).
    assert len(final_outputs) == wf.ran
    assert error_dict == {}
    # events.ndjson exists + parses (crash-durable).
    lines = [ln for ln in (exec_dir / "events.ndjson").read_text().splitlines() if ln.strip()]
    assert lines
    for ln in lines:
        json.loads(ln)


def test_drive_no_marker_runs_fully(tmp_path):
    """Absent marker → full run, zero behavior change (all nodes ran)."""
    import asyncio

    exec_dir = tmp_path / "__exec__"
    exec_dir.mkdir()
    events_path = str(exec_dir / "events.ndjson")
    cancel_path = str(exec_dir / "cancel")  # never created

    wf = _FakeWorkflow(n_nodes=3, per_node_delay=0.0)
    final_outputs, error_dict, _ = asyncio.run(
        sandbox_entry._drive(
            wf, {}, {"run_id": "r1", "run_dir": str(tmp_path)},
            events_path, cancel_path=cancel_path,
        )
    )
    assert wf.ran == 3, "all nodes should have run with no cancel marker"
    assert len(final_outputs) == 3
    assert error_dict == {}
