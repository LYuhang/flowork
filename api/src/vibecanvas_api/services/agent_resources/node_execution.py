"""Select and validate a standalone node without requiring a runnable graph."""
from copy import deepcopy

from vibecanvas_engine.node import node_registry

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources.workflow_graph import validate_workflow_model_names
from vibecanvas_api.services.workflow_model_policy import available_workflow_model_ids


async def select_execution_node(workflow: dict, node_id: str, ctx) -> dict:
    node = workflow.get(node_id)
    if not isinstance(node, dict) or node_id.startswith("__"):
        raise ToolError("node_not_found", f"Node '{node_id}' does not exist in the selected workflow snapshot.")
    node = deepcopy(node)
    if node.get("node_id", node_id) != node_id:
        raise ToolError("invalid_node", "The node_id does not match its workflow dictionary key.")
    node["node_id"] = node_id
    node_type = node.get("node_type")
    if not isinstance(node_type, str) or node_type not in node_registry._module_dict:
        raise ToolError("unknown_node_type", "The selected node type is not supported.")
    if node_type in {"LoopBeginNode", "LoopEndNode", "ParallelStartNode", "ParallelEndNode"}:
        raise ToolError("node_requires_workflow", "Loop and parallel control nodes require workflow scheduling. Run without --node.")
    try:
        result = node_registry._module_dict[node_type].check(node)
    except (ValueError, TypeError, KeyError, AttributeError, AssertionError, RecursionError, NotImplementedError) as exc:
        raise ToolError("invalid_node", "The node configuration is malformed. Inspect workflow get-spec and repair the target node.") from exc
    if isinstance(result, dict) and result.get("status") == "error":
        raise ToolError("invalid_node", result.get("error_message") or "The node configuration is invalid.")
    # Only the chosen node contributes model/dependency/egress requirements.
    # Keep workflow-level package settings, not unrelated node configurations.
    selected = {node_id: node}
    if "__meta__" in workflow:
        if not isinstance(workflow["__meta__"], dict):
            raise ToolError("invalid_workflow", "Workflow metadata must be an object.")
        selected["__meta__"] = deepcopy(workflow["__meta__"])
    if node_type in {"PromptNode", "SubAgentNode"}:
        errors = validate_workflow_model_names(selected, await available_workflow_model_ids(ctx))
        if errors:
            raise ToolError("invalid_node", errors[0]["message"])
    return selected
