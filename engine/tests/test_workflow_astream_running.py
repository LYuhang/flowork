"""The engine emits a per-node running event before each node's
terminal (success/error) event, for EVERY node type incl. LoopBegin/
LoopEnd (which skip ``dispatch_node_call``), and the accumulators
(``_trigger_inner``) IGNORE the non-terminal ``running`` frames so the
final outputs / error_dict are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibecanvas_engine import Workflow


def _example_wf() -> dict:
    p = Path(__file__).resolve().parent / "example_workflow.json"
    return json.loads(p.read_text())


@pytest.mark.asyncio
async def test_running_emitted_before_each_success():
    """Every node that reaches success must have a preceding ``running``
    frame with the same node_id."""
    wf = Workflow(_example_wf(), max_workers=4)

    events = []
    async for ev in wf.astream({"text": "hi", "count": 2}):
        events.append(ev)

    # A running frame must appear for some node, and for every node that
    # succeeded, a running frame for that node must precede its success.
    running_ids = [e.get("node_id") for e in events if e.get("status") == "running"]
    assert running_ids, f"no 'running' frames in stream: {[e.get('status') for e in events]}"

    seen_running: set[str] = set()
    for ev in events:
        nid = ev.get("node_id")
        if ev.get("status") == "running":
            seen_running.add(nid)
        elif ev.get("status") == "success":
            assert nid in seen_running, (
                f"node {nid} emitted success without a preceding running frame"
            )

    # The running frame carries node identity + a null output / empty error.
    first_running = next(e for e in events if e.get("status") == "running")
    assert first_running["node_id"]
    assert first_running["node_name"]
    assert first_running["node_type"]
    assert first_running["output"] is None
    assert first_running["error_message"] == ""
    assert first_running["inputs"] == {"text": "hi", "count": 2}
    started = {e["span_id"]: e for e in events if e.get("status") == "running"}
    for event in events:
        if event.get("status") == "success":
            assert started[event["span_id"]]["inputs"] == event["inputs"]


@pytest.mark.asyncio
async def test_running_does_not_corrupt_accumulation():
    """The ``running`` frames must NOT change the final outputs that
    ``_trigger_inner`` (the accumulator the sync trigger drains) produces.
    """
    wf = Workflow(_example_wf(), max_workers=4)

    # Drain via _trigger_inner directly (the accumulator path).
    previous_outputs, error_dict, _ = await wf._trigger_inner(
        {"text": "hi", "count": 2}
    )
    assert not error_dict, f"unexpected errors: {error_dict}"
    assert previous_outputs.get("__end__") == {
        "repeated": "hi hi",
        "char_count": 5,
    }, f"running frames corrupted the accumulator: {previous_outputs}"


def _loop_wf() -> dict:
    """Start → LoopBegin → CodeNode → LoopEnd → End. Exercises the
    LoopBegin/LoopEnd branches that SKIP ``dispatch_node_call`` — they
    must still emit ``running`` because emission occurs before the node-type
    branches)."""
    return {
        "__meta__": {
            "workflow_id": "wf_loop",
            "workflow_name": "loop_running",
            "workflow_version": 1,
            "workflow_subversion": 0,
        },
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "StartNode",
            "node_description": "",
            "input_fields": {},
            "output_fields": {},
            "node_config": {},
            "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2",
            "node_name": "lb",
            "node_type": "LoopBeginNode",
            "node_description": "",
            "input_fields": {},
            "output_fields": {},
            "node_config": {
                "init_value": {"value": 0, "reference": ""},
                "end_value": {"value": 3, "reference": ""},
                "step_value": 1,
                "loop_end_node_id": "node_4",
            },
            "children": ["node_3"],
        },
        "node_3": {
            "node_id": "node_3",
            "node_name": "body",
            "node_type": "CodeNode",
            "node_description": "",
            "input_fields": {},
            "output_fields": {"y": {"type": "integer", "description": ""}},
            "node_config": {
                "programming_language": "python",
                "process_fn": "def process_fn(inputs):\n    return {'y': 1}",
            },
            "children": ["node_4"],
        },
        "node_4": {
            "node_id": "node_4",
            "node_name": "le",
            "node_type": "LoopEndNode",
            "node_description": "",
            "input_fields": {},
            "output_fields": {},
            "node_config": {"loop_begin_node_id": "node_2"},
            "children": ["node_5"],
        },
        "node_5": {
            "node_id": "node_5",
            "node_name": "__end__",
            "node_type": "EndNode",
            "node_description": "",
            "input_fields": {},
            "output_fields": {},
            "node_config": {},
            "children": [],
        },
    }


@pytest.mark.asyncio
async def test_running_emitted_for_loop_begin_and_end():
    """LoopBegin (node_2) and LoopEnd (node_4) skip ``dispatch_node_call``
    yet must still surface a ``running`` frame before the node-type branches."""
    wf = Workflow(_loop_wf(), max_workers=4)

    events = []
    async for ev in wf.astream({}):
        events.append(ev)

    finished = next((e for e in events if e.get("status") == "finished"), None)
    assert finished is not None, f"no finished: {[e.get('status') for e in events]}"
    assert not finished.get("error_dict"), f"errors: {finished['error_dict']}"

    running_ids = {e.get("node_id") for e in events if e.get("status") == "running"}
    assert "node_2" in running_ids, "LoopBeginNode never emitted a running frame"
    assert "node_4" in running_ids, "LoopEndNode never emitted a running frame"


@pytest.mark.asyncio
async def test_interrupted_node_keeps_resolved_input_snapshot(monkeypatch):
    import asyncio
    import importlib
    trigger = importlib.import_module("vibecanvas_engine.nodes.trigger")
    original = trigger.dispatch_node_call

    async def blocked(node, inputs, previous_outputs, extra):
        if node.node_type == "CodeNode":
            await asyncio.Event().wait()
        return await original(node, inputs, previous_outputs, extra=extra)

    monkeypatch.setattr(trigger, "dispatch_node_call", blocked)
    stream = Workflow(_example_wf()).astream({"text": "interrupted", "count": 7})
    events = []
    async with asyncio.timeout(5):
        try:
            async for event in stream:
                events.append(event)
                if event.get("node_type") == "CodeNode" and event["status"] == "running":
                    assert event["inputs"] == {"text": "interrupted", "count": 7}
                    break
        finally:
            await stream.aclose()
    assert not any(e.get("node_type") == "CodeNode" and e["status"] == "success" for e in events)
