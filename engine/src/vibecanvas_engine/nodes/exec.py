# -*- coding: utf-8 -*-
"""Single-node execution helpers.

``dispatch_node_call`` is the per-node ``__call__`` + thread-bridge dispatch,
shared by the engine's full-graph ``trigger`` and by ``run_node``. ``run_node``
is the reusable standalone single-node runner (the workflow's node-debug
primitive): it seeds inputs from the node's configured ``input_fields`` values
(overlaid with caller-supplied ``inputs``), then dispatches. NO reference
resolution and NO ``previous_outputs`` — the caller routes data.
"""
from __future__ import annotations

import asyncio

from ..register import node_registry
from .base import BaseNode


class UnknownNodeType(Exception):
    """Raised by run_node when node_type is not in the registry."""


async def dispatch_node_call(node: BaseNode, inputs: dict, previous_outputs: dict, extra=None) -> dict:
    """Run ONE node's __call__ with the correct async/thread bridge.

    - nodes declaring ``REQUIRES_THREAD_BRIDGE = True`` → ``asyncio.to_thread``
      (their sync bodies block — e.g. CodeNode waits on its worker pool, or other
      nodes internally call ``asyncio.run`` — and would stall the engine's running
      loop if run inline).
    - everything else → inline sync ``__call__``.
    Returns the node's ``{status, output, ...}`` result dict.
    """
    local_extra = dict(extra or {}) if node.node_type == "SubAgentNode" else (extra if extra is not None else {})
    if hasattr(node, "call_async"):
        result = await node.call_async(inputs, previous_outputs, local_extra)
    elif getattr(node, "REQUIRES_THREAD_BRIDGE", False):
        # Establish Code pool ownership before starting its thread. A cancelled
        # await must not race a late pool creation and leave user code alive.
        pool = node._get_run_pool(local_extra) if node.node_type == "CodeNode" else None
        operation = asyncio.create_task(asyncio.to_thread(node, inputs, previous_outputs, extra=local_extra))
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError:
            if pool is not None:
                pool.close()
            # Thread-backed file operations must actually finish before the
            # invocation is declared stopped. Cancelling an await alone is not
            # sufficient evidence that side effects stopped.
            await asyncio.shield(operation)
            raise
    else:
        return node(inputs, previous_outputs)
    if node.node_type == "SubAgentNode":
        if '_subagent_resource_audit' in local_extra:
            result['resource_audit'] = local_extra['_subagent_resource_audit']
        if '_subagent_traces' in local_extra:
            if not isinstance(result.get('output'), dict):
                result['output'] = {}
            result['output']['__traces__'] = local_extra['_subagent_traces']
    return result


async def run_node(node_dict: dict, inputs: dict | None = None, extra: dict | None = None) -> dict:
    """Run ONE node standalone from its node_dict.

    Seeds the call inputs from the node's configured ``input_fields[*].value``,
    overlaid with caller-supplied ``inputs`` (caller wins). NO reference
    resolution / NO previous_outputs — the caller routes data. Returns the
    node's raw ``{status, output, ...}`` result dict; node-execution errors come
    back IN the dict (via safe_call_with_args), NOT raised. Only an unknown
    node_type raises ``UnknownNodeType``.
    """
    node_type = node_dict.get("node_type")
    try:
        node_cls = node_registry.get(node_type)
    except KeyError:
        raise UnknownNodeType(node_type)
    node = node_cls(**node_dict)
    effective = {name: f.get("value") for name, f in (node_dict.get("input_fields") or {}).items()}
    if inputs:
        effective.update(inputs)
    return await dispatch_node_call(node, effective, {}, extra or {})
