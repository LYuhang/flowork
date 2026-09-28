"""Credential-free workflow admission for isolated sandbox execution.

Only engine-native node types may enter a workflow sandbox. Platform data
access belongs behind host brokers/Platform MCP; an API node must never become
sandbox-runnable merely because it can use an RLS-scoped database role.
"""
from __future__ import annotations

from vibecanvas_engine.nodes import ENGINE_PURE_NODE_TYPES

from .gvisor import EngineNeedsHostNode


SANDBOX_RUNNABLE_NODE_TYPES: frozenset[str] = frozenset(
    ENGINE_PURE_NODE_TYPES
)


def classify_workflow(workflow: dict) -> str:
    """Return ``pure`` or reject the first host-only/unknown node.

    There is intentionally no ``db`` result and no trusted-workflow exception:
    ownership/RLS is not a safe substitute for keeping host credentials out of
    the sandbox.
    """
    for node_id, node in workflow.items():
        if node_id == "__meta__":
            continue
        node_type = (node or {}).get("node_type")
        if node_type not in SANDBOX_RUNNABLE_NODE_TYPES:
            raise EngineNeedsHostNode(node_type)
    return "pure"
