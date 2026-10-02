"""Successful visits used by explicit workflow-page retries, never live recovery."""

import json


CONTROL_NODES = frozenset({"LoopBeginNode", "LoopEndNode", "ParallelStartNode", "ParallelEndNode"})


def visit_key(node_id, loop_stack):
    return json.dumps([node_id, [[f["begin_node_id"], f["iter_index"]] for f in loop_stack]], separators=(",", ":"))


def successful_visits(events):
    return {
        visit_key(e["node_id"], e.get("loop_stack", [])): {"inputs": e.get("inputs", {}), "output": e.get("output", {})}
        for e in events
        if e.get("type") == "node_event" and e.get("status") == "success"
        and e.get("node_type") not in CONTROL_NODES
    }
