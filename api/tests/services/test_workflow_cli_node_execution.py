"""Standalone node validation must not require a complete runnable graph."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources import node_execution as nodes


def start_node():
    return {"node_id": "node_1", "node_name": "__start__", "node_type": "StartNode",
            "node_description": "", "node_config": {}, "input_fields": {}, "output_fields": {},
            "children": ["missing_downstream"]}


@pytest.mark.asyncio
async def test_target_validation_ignores_unrelated_graph_and_models(monkeypatch):
    models = AsyncMock(side_effect=AssertionError("non-model node needs no model lookup"))
    monkeypatch.setattr(nodes, "available_workflow_model_ids", models)
    graph = {"node_1": start_node(), "broken": None, "other": {"node_type": "PromptNode"},
             "__meta__": {"settings": {"requirements": []}}}
    selected = await nodes.select_execution_node(graph, "node_1", object())
    assert set(selected) == {"node_1", "__meta__"}
    assert selected["node_1"] == graph["node_1"] and selected["node_1"] is not graph["node_1"]
    models.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("node_id,node,code", [
    ("missing", None, "node_not_found"),
    ("node_1", {"node_type": "Unknown"}, "unknown_node_type"),
    ("node_1", {"node_type": "StartNode", "node_id": "node_2"}, "invalid_node"),
    ("node_1", {"node_type": "StartNode"}, "invalid_node"),
    ("node_1", {"node_type": "LoopBeginNode"}, "node_requires_workflow"),
    ("node_1", {"node_type": "ParallelEndNode"}, "node_requires_workflow"),
])
async def test_invalid_or_scheduler_dependent_nodes_report_explicit_errors(node_id, node, code):
    with pytest.raises(ToolError, match=code):
        await nodes.select_execution_node({node_id: node}, node_id, object())


@pytest.mark.asyncio
async def test_target_model_permission_is_rechecked(monkeypatch):
    monkeypatch.setitem(nodes.node_registry._module_dict, "PromptNode", SimpleNamespace(check=lambda _: {"status": "success"}))
    models = AsyncMock(return_value={"allowed"})
    monkeypatch.setattr(nodes, "available_workflow_model_ids", models)
    graph = {"node_1": {"node_type": "PromptNode", "node_config": {"model_name": "revoked"}}}
    with pytest.raises(ToolError, match="invalid_node"):
        await nodes.select_execution_node(graph, "node_1", object())
    assert models.await_count == 1
