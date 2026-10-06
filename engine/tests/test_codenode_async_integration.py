"""CodeNode runs through the per-run CodeWorkerPool via the unified
thread bridge.

Contract:
  - A workflow with a CodeNode runs end-to-end; its output lands in
    previous_outputs (the worker-pool path is exercised by the engine run).
  - CodeNode no longer has a dedicated ``call_async`` dispatch branch — it flows
    through the unified ``REQUIRES_THREAD_BRIDGE`` → ``asyncio.to_thread`` path.
"""

from __future__ import annotations

import threading

import pytest

from vibecanvas_engine.workflow import Workflow
from vibecanvas_engine.nodes.code import CodeNode


@pytest.mark.asyncio
async def test_codenode_runs_in_worker_pool(simple_codenode_workflow_dict, tmp_path):
    """A workflow with a CodeNode runs end-to-end; result lands in previous_outputs."""
    wf = Workflow(simple_codenode_workflow_dict)
    events = [
        ev
        async for ev in wf.astream({}, run_context={"run_id": "t", "run_dir": str(tmp_path)})
    ]
    finished = next(e for e in events if e.get("status") == "finished")
    # The fixture's CodeNode returns {"answer": 42}; assert that surfaces.
    assert any(
        v.get("answer") == 42
        for v in finished.get("final_outputs", {}).values()
        if isinstance(v, dict)
    ), f"Expected CodeNode to yield answer=42; got {finished.get('final_outputs')}"


def test_codenode_declares_thread_bridge():
    """CodeNode opts into the unified off-loop dispatch (no call_async branch)."""
    assert getattr(CodeNode, "REQUIRES_THREAD_BRIDGE", False) is True
    assert not hasattr(CodeNode, "call_async"), (
        "call_async must not remain on CodeNode"
    )


@pytest.mark.asyncio
async def test_dispatch_runs_code_off_loop_and_prepares_pool_on_loop():
    """Code stays off-loop while its pool is owned before execution starts."""
    from vibecanvas_engine.nodes.exec import dispatch_node_call

    loop_thread = threading.get_ident()
    calls = []

    class CodeProbe:
        node_type = "CodeNode"
        REQUIRES_THREAD_BRIDGE = True

        def _get_run_pool(self, extra):
            calls.append(("pool", threading.get_ident()))
            return None

        def __call__(self, inputs, previous_outputs, *, extra):
            calls.append(("call", threading.get_ident()))
            return {"status": "success", "output": inputs}

    result = await dispatch_node_call(CodeProbe(), {"answer": 42}, {})
    assert result["output"] == {"answer": 42}
    assert calls[0] == ("pool", loop_thread)
    assert calls[1][0] == "call"
    assert calls[1][1] != loop_thread
