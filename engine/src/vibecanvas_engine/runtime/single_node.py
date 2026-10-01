"""Standalone node events using the same runtime ownership as full graphs.

Children and references are deliberately not traversed. Workflow construction
only creates the selected node and applies workflow-level timeout defaults.
"""

import asyncio
from copy import deepcopy
import time

from ..nodes.exec import dispatch_node_call
from ..register import node_registry


def validate_selected_node(workflow: dict, node_id: str) -> None:
    if not isinstance(node_id, str) or node_id.startswith("__"):
        raise ValueError("invalid selected node")
    if set(workflow) - {"__meta__"} != {node_id}:
        raise ValueError("standalone execution requires exactly the selected node")
    node = workflow[node_id]
    if not isinstance(node, dict) or node.get("node_id") != node_id:
        raise ValueError("selected node ID mismatch")
    kind = node.get("node_type")
    if kind in {"LoopBeginNode", "LoopEndNode", "ParallelStartNode", "ParallelEndNode"}:
        raise ValueError("control node requires workflow scheduling")
    validation = node_registry.get(kind).check(node)
    if validation.get("status") != "success":
        raise ValueError(validation.get("error_message") or "invalid selected node")


async def node_events(workflow, node_id: str, inputs: dict, *, stop_event, run_context):
    started = time.monotonic()
    node = workflow.id2node[node_id]
    definition = workflow._workflow_dict[node_id]
    effective = {name: deepcopy(field.get("value")) for name, field in definition.get("input_fields", {}).items()}
    effective.update(inputs)
    run_context["stop_event"] = stop_event
    # Allocate the pool owner before dispatch so cancellation cannot race the
    # worker thread lazily creating a pool after cleanup has already run.
    code_pool = node._get_run_pool(run_context) if node.node_type == "CodeNode" else None
    operation = stopped = None
    try:
        yield {"node_id": node_id, "status": "running", "inputs": effective}
        operation = asyncio.create_task(dispatch_node_call(node, effective, {}, run_context))
        stopped = asyncio.create_task(stop_event.wait())
        await asyncio.wait({operation, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if stop_event.is_set():
            return
        try:
            result = await operation
        except Exception as exc:
            result = {"status": "error", "error_message": str(exc)}
        success = result.get("status") == "success"
        message = result.get("error_message") or ("" if success else "node_execution_failed")
        yield {**result, "node_id": node_id, "status": "success" if success else "error", "error_message": message}
        yield {
            "status": "finished",
            "final_outputs": {node_id: result.get("output")} if success else {},
            "error_dict": {} if success else {node_id: message},
            "execution_time": time.monotonic() - started,
        }
    finally:
        stop_event.set()
        if code_pool is not None:
            code_pool.close()
        # Cancelling to_thread does not stop its thread. Await cooperative
        # shutdown (or the node's own timeout) before publishing a final result.
        # The host can still forcibly stop this isolated process when needed.
        if stopped is not None:
            stopped.cancel()
        await asyncio.gather(*(task for task in (operation, stopped) if task is not None), return_exceptions=True)
