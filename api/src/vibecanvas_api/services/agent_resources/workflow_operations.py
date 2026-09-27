"""Ordered Chat-branch edits: atomic steps, one durable successful-prefix commit."""
import re
from copy import deepcopy

from jsonschema import Draft202012Validator

from vibecanvas_api.agents.prompts.node_definitions import available_node_types, build_node_spec
from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.flowork_cli.cli import error, validate_arguments
from vibecanvas_api.services.agent_resources.authorization import (
    _decision,
    _require_active_chat_write,
    _service,
    _workflow_resource,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

from .workflow_target import resolve_target


def _node_id(value):
    if not isinstance(value, str) or not value.strip() or value.startswith("__"):
        raise ToolError("invalid_node_id", "Node IDs must be nonblank strings and cannot begin with '__'.")
    return value


def _node(graph, node_id):
    _node_id(node_id)
    if node_id not in graph:
        raise ToolError("node_not_found", f"Node '{node_id}' does not exist.")
    if not isinstance(graph[node_id], dict):
        raise ToolError("invalid_node", f"Node '{node_id}' must be an object.")
    return graph[node_id]


def _children(node):
    children = node.get("children", [])
    if not isinstance(children, list) or any(not isinstance(child, str) for child in children):
        raise ToolError("invalid_node", "children must be an array of node ID strings.")
    return children


def _config_types(value, schema, path):
    """Check known types only; completeness and semantic validation belong to check.

    Do not infer types from existing values: an optional value may legitimately
    change from null to an object. Unknown/custom config fields remain editable.
    """
    if not isinstance(schema, dict):
        return
    if "type" in schema:
        errors = list(Draft202012Validator({"type": schema["type"]}).iter_errors(value))
        if errors:
            raise ToolError("invalid_value", f"Invalid value type at {path}; expected {schema['type']}.")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key, child in value.items():
            _config_types(child, properties.get(key, schema.get("additionalProperties", {})), f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _config_types(child, schema.get("items", {}), f"{path}/{index}")


def _validate_node(node_id, node):
    if not isinstance(node, dict):
        raise ToolError("invalid_node", "The new node must be a JSON object.")
    if node.get("node_id", node_id) != node_id:
        raise ToolError("invalid_node", "node_id must match the workflow dictionary key.")
    node_type = node.get("node_type")
    if not isinstance(node_type, str) or node_type not in available_node_types():
        raise ToolError("unknown_node_type", "Use a supported node_type from workflow get-spec --list-types.")
    for field in ("node_config", "input_fields", "output_fields", "__attributes__"):
        if field in node and not isinstance(node[field], dict):
            raise ToolError("invalid_value", f"{field} must be an object.")
    if "node_description" in node and not isinstance(node["node_description"], str):
        raise ToolError("invalid_value", "node_description must be a string.")
    _children(node)
    _config_types(node.get("node_config", {}), build_node_spec(node_type)["config_schema"], "/node_config")


def apply_operation(graph: dict, operation: dict) -> dict:
    """Never mutate the caller's graph, including when a step fails midway."""
    updated = deepcopy(graph)
    op = operation["op"]
    if op == "node_add":
        node = deepcopy(operation["node"])
        if not isinstance(node, dict):
            raise ToolError("invalid_node", "The new node must be a JSON object.")
        node_id = _node_id(node.get("node_id"))
        if node_id in updated:
            raise ToolError("node_exists", f"Node '{node_id}' already exists.")
        node.setdefault("node_config", {})
        node.setdefault("children", [])
        _validate_node(node_id, node)
        children = _children(node)
        if len(children) != len(set(children)):
            raise ToolError("edge_exists", "The new node contains duplicate children edges.")
        for target in children:
            if target == node_id:
                raise ToolError("invalid_edge", "Self edges are not supported.")
            _node(updated, target)
        updated[node_id] = node
    elif op == "node_remove":
        node_id = operation["node_id"]
        _node(updated, node_id)
        del updated[node_id]
        for key, node in updated.items():
            if key.startswith("__"):
                continue
            children = _children(_node(updated, key))
            if node_id in children:
                node["children"] = [child for child in children if child != node_id]
    elif op == "node_update":
        path = operation["path"]
        if not path.startswith("/") or re.search(r"~(?![01])", path):
            raise ToolError("invalid_path", "Use a JSON Pointer starting with '/', escaping '~' as '~0' and '/' as '~1'.")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
        if len(parts) < 2:
            raise ToolError("invalid_path", "Specify a field inside a node; replacing the entire node is not supported.")
        node_id, field = parts[:2]
        node = _node(updated, node_id)
        if field in {"node_id", "node_type", "children"} or (field.startswith("__") and field != "__attributes__"):
            raise ToolError("protected_field", "Cannot update node identity/type, children, or reserved metadata through node_update.")
        parent = node
        for part in parts[1:-1]:
            if not isinstance(parent, dict) or part not in parent:
                raise ToolError("path_not_found", "Every parent object in the update path must exist.")
            parent = parent[part]
        if not isinstance(parent, dict):
            raise ToolError("invalid_path", "The update parent must be an object; replace arrays as a whole.")
        parent[parts[-1]] = deepcopy(operation["value"])
        _validate_node(node_id, node)
    elif op in {"edge_add", "edge_remove"}:
        source, target = operation["source"], operation["target"]
        node = _node(updated, source)
        _node(updated, target)
        children = _children(node)
        if op == "edge_add":
            if source == target:
                raise ToolError("invalid_edge", "Self edges are not supported.")
            if target in children:
                raise ToolError("edge_exists", f"Edge '{source}' -> '{target}' already exists.")
            node["children"] = [*children, target]
        else:
            if target not in children:
                raise ToolError("edge_not_found", f"Edge '{source}' -> '{target}' does not exist.")
            node["children"] = [child for child in children if child != target]
    else:
        raise ToolError("invalid_operation", "Unsupported workflow operation.")
    return updated


async def operate_workflow(ctx, arguments: dict) -> dict:
    arguments = validate_arguments("workflow.operation", arguments)
    operations = arguments["operations"]
    async with session_scope(tenant_id=ctx.tenant_id) as session:
        # Lock before reading: serializes edits with upload/version changes and
        # avoids stale read-modify-write snapshots. Lock order: Run -> Chat -> WF.
        await _require_active_chat_write(session, ctx)
        selection = await resolve_target(session, ctx, arguments["workflow_id"], arguments["major"], for_update=True)
        workflow_id, major, sub = selection["id"], selection["major"], selection["sub"]
        await _decision(ctx=ctx, service=_service(ctx, session), action=Action.UPDATE,
                        resource=_workflow_resource(ctx, workflow_id),
                        consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        repo = WorkflowRepo(session, ctx.username)
        graph = deepcopy(await repo.get_workflow_at(workflow_id, major, sub))
        applied, failure, results = 0, {}, []
        for index, operation in enumerate(operations):
            try:
                graph = apply_operation(graph, operation)
            except ToolError as exc:
                failure = {**error(str(exc), exc.message, "Inspect the saved workflow, fix the failed operation, and submit only the remaining operations. Do not replay the saved prefix."),
                           "failed_index": index}
                results.append({"index": index, "op": operation["op"], "status": "failed",
                                "error": str(exc), "message": exc.message})
                break
            applied += 1
            results.append({"index": index, "op": operation["op"], "status": "saved"})
        if applied:
            pointer = await repo.commit(workflow_id, graph, note=arguments["note"] or f"agent: workflow operation {applied}/{len(operations)}",
                                        target_major=major, stamp_metadata=True)
            sub = pointer.sv
        result = {"id": workflow_id, "version": f"v{major}.sv{sub}", "applied": applied,
                  "total": len(operations), "skipped": len(operations) - len(results), "results": results,
                  "message": f"Saved {applied} operation(s)." if applied else "No operations were saved.", **failure}
    # Never expose 'saved' progress before the transaction actually commits.
    # Commit/transport failures must surface as result_unknown at the Host.
    return result
