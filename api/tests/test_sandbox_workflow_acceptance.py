"""gVisor acceptance for a real multi-node workflow with a parallel
block runs end-to-end INSIDE the gVisor sandbox, and BOTH success results AND
node errors propagate OUT of the sandbox.

Shape: ``start → ParallelStart → code → ParallelEnd → end`` (single branch is
enough to exercise the parallel split/join + the asyncio.create_task spawn path
inside the sandbox).

The ParallelStart/End schema is the proven one from
``engine/tests/test_workflow_astream.py`` + the node CONFIG_SCHEMAs:
  - ParallelStartNode.node_config = {
        "branches": {<name>: {"branch_description": str, "next_node_id": <id>}},
        "parallel_end_node_id": <ParallelEndNode id>,
    }  (input_fields/output_fields MUST be empty; non-empty next_node_ids ==
        children)
  - ParallelEndNode.node_config = {"parallel_start_node_id": <ParallelStart id>}
        (input_fields/output_fields MUST be empty; children has at most 1 item)

Sanity: the workflow was confirmed valid in-process (Workflow(wf).trigger(
{"x": 21}) → __end__.v == 42) BEFORE the gVisor run.

NOTE (sandbox builtins whitelist): ``RuntimeError`` is NOT whitelisted in the
CodeNode sandbox; the error branch raises ``Exception('branch-boom')``.
"""

import pytest

from vibecanvas_api.services.sandbox import get_sandbox_provider, _gvisor_runnable

gvisor = pytest.mark.skipif(not _gvisor_runnable(), reason="rootless gVisor not runnable here")


def _meta():
    return {"workflow_id": "wacc", "workflow_name": "acc", "workflow_version": 1, "workflow_subversion": 0}


def _parallel_wf(process_fn: str) -> dict:
    """start → ParallelStart → code → ParallelEnd → end, built from the PROVEN
    ParallelStart/End schema. The single CodeNode branch runs ``process_fn``;
    the End node outputs ``v`` (traced back: worker.v → __end__.v)."""
    return {
        "__meta__": _meta(),
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "StartNode",
            "node_description": "start",
            "input_fields": {},
            "output_fields": {"x": {"type": "integer", "description": "input n"}},
            "node_config": {},
            "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2",
            "node_name": "psplit",
            "node_type": "ParallelStartNode",
            "node_description": "split into one parallel branch",
            "input_fields": {},
            "output_fields": {},
            "node_config": {
                "branches": {
                    "a": {"branch_description": "compute branch", "next_node_id": "node_3"},
                },
                "parallel_end_node_id": "node_4",
            },
            "children": ["node_3"],
        },
        "node_3": {
            "node_id": "node_3",
            "node_name": "worker",
            "node_type": "CodeNode",
            "node_description": "compute v from x",
            "input_fields": {
                "x": {"type": "integer", "value": 0, "reference": "__start__.x"},
            },
            "output_fields": {"v": {"type": "integer", "description": "result"}},
            "node_config": {
                "programming_language": "python",
                "process_fn": process_fn,
            },
            "children": ["node_4"],
        },
        "node_4": {
            "node_id": "node_4",
            "node_name": "pmerge",
            "node_type": "ParallelEndNode",
            "node_description": "join parallel branch",
            "input_fields": {},
            "output_fields": {},
            "node_config": {"parallel_start_node_id": "node_2"},
            "children": ["node_5"],
        },
        "node_5": {
            "node_id": "node_5",
            "node_name": "__end__",
            "node_type": "EndNode",
            "node_description": "end",
            "input_fields": {
                "v": {"type": "integer", "value": 0, "reference": "worker.v"},
            },
            "output_fields": {"v": {"type": "integer", "description": "result"}},
            "node_config": {},
            "children": [],
        },
    }


@gvisor
def test_parallel_workflow_runs_in_sandbox_and_returns_result(tmp_path):
    wf = _parallel_wf("def process_fn(inputs):\n    return {'v': inputs['x'] * 2}")
    res = get_sandbox_provider().run_workflow(
        run_dir=str(tmp_path), workflow=wf, inputs={"x": 21}, run_id="wacc-ok", timeout=120.0)
    assert res.error_dict == {}, f"engine errors: {res.error_dict}\nstderr={getattr(res.sandbox,'stderr','')[-800:]}"
    assert res.final_outputs.get("__end__", {}).get("v") == 42
    assert isinstance(res.events, list) and res.events, "events did not propagate out"


@gvisor
def test_parallel_workflow_error_propagates_out(tmp_path):
    wf = _parallel_wf("def process_fn(inputs):\n    raise Exception('branch-boom')")
    res = get_sandbox_provider().run_workflow(
        run_dir=str(tmp_path), workflow=wf, inputs={"x": 1}, run_id="wacc-err", timeout=120.0)
    assert res.error_dict, "a raising CodeNode in a parallel branch must surface in error_dict"
    assert "branch-boom" in str(res.error_dict)


@pytest.fixture(autouse=True)
def select_gvisor_backend(monkeypatch):
    """These guarded tests exercise the explicitly selected gVisor backend."""
    from vibecanvas_api.config import config
    monkeypatch.setattr(config, "sandbox_runtime", "gvisor")
