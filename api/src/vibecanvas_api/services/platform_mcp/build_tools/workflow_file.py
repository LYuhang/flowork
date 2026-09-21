"""Shared helpers for Platform MCP file-oriented workflow tools."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime

from vibecanvas_api.agents.tools._session_fs import _require_session
from vibecanvas_api.services.platform_mcp.build_tools._target import target_workflow_id
from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_engine.node import node_registry
from vibecanvas_engine.workflow import Workflow

DEFAULT_WORKFLOW_PATH = "/data/workflow.json"
_NODE_WIDTH = 220
_NODE_HEIGHT = 100
_NODE_SEP = 60
_RANK_SEP = 80
_NODE_X_STEP = _NODE_WIDTH + _RANK_SEP
_NODE_Y_STEP = _NODE_HEIGHT + _NODE_SEP
_NODE_ID_RE = re.compile(r"^(.*?)(\d+)$")


def _node_count(workflow: dict) -> int:
    return sum(
        1 for key, value in workflow.items()
        if isinstance(key, str) and not key.startswith("__") and isinstance(value, dict)
    )


def _node_sort_key(node_id: str) -> tuple[str, int, str]:
    match = _NODE_ID_RE.match(node_id)
    if match:
        return match.group(1), int(match.group(2)), node_id
    return node_id, 0, node_id


def _workflow_node_ids(workflow: dict) -> list[str]:
    return sorted(
        [
            key for key, value in workflow.items()
            if isinstance(key, str) and not key.startswith("__") and isinstance(value, dict)
        ],
        key=_node_sort_key,
    )


def _workflow_edges(workflow: dict, node_ids: set[str]) -> tuple[dict[str, list[str]], dict[str, int]]:
    children_by_node = {node_id: [] for node_id in node_ids}
    indegree = {node_id: 0 for node_id in node_ids}
    for node_id in sorted(node_ids, key=_node_sort_key):
        node = workflow.get(node_id)
        if not isinstance(node, dict):
            continue
        children = node.get("children")
        if not isinstance(children, list):
            continue
        for child in children:
            if isinstance(child, str) and child in node_ids:
                children_by_node[node_id].append(child)
                indegree[child] += 1
    for node_id in children_by_node:
        children_by_node[node_id].sort(key=_node_sort_key)
    return children_by_node, indegree


def _layout_ranks(workflow: dict, node_ids: list[str]) -> dict[str, int]:
    """Return left-to-right ranks from graph edges.

    This mirrors the front-end dagre posture (`rankdir: LR`) without pulling a
    JS layout dependency into the API. Nodes with no parents start at rank 0;
    every child is placed at least one rank to the right of its parents. Cycles
    should already be rejected by validation, but any remaining unvisited nodes
    still get deterministic fallback ranks.
    """
    node_set = set(node_ids)
    children_by_node, indegree = _workflow_edges(workflow, node_set)
    ranks = {node_id: 0 for node_id in node_ids}
    ready = sorted([node_id for node_id in node_ids if indegree[node_id] == 0], key=_node_sort_key)
    visited: set[str] = set()

    while ready:
        node_id = ready.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        for child in children_by_node.get(node_id, []):
            ranks[child] = max(ranks.get(child, 0), ranks[node_id] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=lambda nid: (ranks.get(nid, 0), _node_sort_key(nid)))

    fallback_rank = (max(ranks.values()) + 1) if ranks else 0
    for node_id in node_ids:
        if node_id not in visited:
            ranks[node_id] = fallback_rank
            fallback_rank += 1
    return ranks


def _auto_tidy_workflow(workflow: dict) -> int:
    """Apply a deterministic left-to-right display layout to workflow nodes.

    This only updates visual position fields. It keeps the workflow graph and
    node configs unchanged.

    Nodes keep the vertical lane inherited from their parent(s).  Re-centering
    every rank independently looks reasonable for a simple diamond, but it
    makes a short parallel branch's long edge run straight through nodes in a
    longer sibling branch.  That is especially misleading for a CodeNode: the
    crossing line appears to be a second parent even though the persisted edge
    targets the later ParallelEndNode.  Parent barycentres plus deterministic
    collision spreading give branches stable lanes until they merge.
    """
    node_ids = _workflow_node_ids(workflow)
    ranks = _layout_ranks(workflow, node_ids)
    children_by_node, _ = _workflow_edges(workflow, set(node_ids))
    parents_by_node: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for parent_id, children in children_by_node.items():
        for child_id in children:
            parents_by_node[child_id].append(parent_id)
    for parents in parents_by_node.values():
        parents.sort(key=_node_sort_key)

    layers: dict[int, list[str]] = {}
    for node_id in node_ids:
        layers.setdefault(ranks[node_id], []).append(node_id)
    for layer_nodes in layers.values():
        layer_nodes.sort(key=_node_sort_key)

    y_by_node: dict[str, float] = {}
    changed = 0
    for rank in sorted(layers):
        layer_nodes = layers[rank]
        desired: list[tuple[str, float]] = []
        for node_id in layer_nodes:
            parent_lanes = [
                y_by_node[parent_id]
                for parent_id in parents_by_node[node_id]
                if parent_id in y_by_node
            ]
            target_y = sum(parent_lanes) / len(parent_lanes) if parent_lanes else 0.0
            desired.append((node_id, target_y))

        # Stable barycentric ordering reduces crossings.  Enforce the minimum
        # node separation in a forward pass, then translate the whole layer so
        # its centre remains at the desired barycentre.  For three siblings
        # targeting y=0 this yields -step, 0, +step; a single-child chain keeps
        # exactly its parent's lane instead of jumping back to y=0.
        desired.sort(key=lambda item: (item[1], _node_sort_key(item[0])))
        placed: list[tuple[str, float]] = []
        for node_id, target_y in desired:
            next_y = target_y
            if placed:
                next_y = max(next_y, placed[-1][1] + _NODE_Y_STEP)
            placed.append((node_id, next_y))
        if placed:
            desired_mean = sum(target for _, target in desired) / len(desired)
            placed_mean = sum(y for _, y in placed) / len(placed)
            shift = desired_mean - placed_mean
            placed = [(node_id, y + shift) for node_id, y in placed]

        for node_id, y in placed:
            y_by_node[node_id] = y
            node = workflow.get(node_id)
            if not isinstance(node, dict):
                continue
            next_pos = {
                "x": rank * _NODE_X_STEP,
                "y": y,
            }
            attrs = node.get("__attributes__")
            if not isinstance(attrs, dict):
                attrs = {}
            current_pos = {"x": attrs.get("x"), "y": attrs.get("y")}
            if current_pos != next_pos:
                node["__attributes__"] = {**attrs, **next_pos}
                changed += 1
    return changed


def _stamp_meta(ctx, wf_id: str, workflow: dict) -> dict:
    stamped = deepcopy(workflow)
    get_meta = getattr(ctx.repo, "get_meta", None)
    meta = get_meta(wf_id) if callable(get_meta) else None
    wm = stamped.setdefault("__meta__", {})
    wm["workflow_id"] = wf_id
    if meta:
        wm["workflow_name"] = meta.get("workflow_name", wm.get("workflow_name"))
        wm["workflow_version"] = meta.get("active_major", wm.get("workflow_version"))
        wm["workflow_subversion"] = meta.get("active_sub", wm.get("workflow_subversion"))
    return stamped


def _parse_workflow_json(text: str, *, source: str) -> dict:
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ToolError("bad_json", f"{source} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ToolError("bad_workflow", f"{source} must contain a JSON object workflow")
    return data


async def read_text_file(runtime: ToolRuntime, path: str) -> str:
    session = await _require_session(runtime.context)
    res = await session.read_file(path)
    if not res.get("ok"):
        err = res.get("error") or "read failed"
        if err == "not_found":
            raise ToolError("path_not_found", f"path {path!r} does not exist")
        if err == "path_outside_roots":
            raise ToolError("invalid_path", f"path {path!r} is outside the allowed roots")
        raise ToolError("read_failed", f"could not read {path!r}: {err}")
    if res.get("kind") == "binary":
        raise ToolError("bad_file", f"path {path!r} is a binary file, expected JSON text")
    return res.get("content", "")


async def read_workflow_file(runtime: ToolRuntime, path: str) -> dict:
    return _parse_workflow_json(await read_text_file(runtime, path), source=path)


async def write_text_file(runtime: ToolRuntime, path: str, content: str) -> None:
    session = await _require_session(runtime.context)
    res = await session.write_file(path, content)
    if not res.get("ok"):
        err = res.get("error") or "write failed"
        if err == "path_outside_roots":
            raise ToolError("invalid_path", f"path {path!r} is outside the allowed roots")
        raise ToolError("write_failed", f"could not write {path!r}: {err}")


async def write_json_file(runtime: ToolRuntime, path: str, payload: Any) -> None:
    await write_text_file(
        runtime,
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def validate_workflow(workflow: dict) -> list[dict]:
    errors: list[dict] = []
    wf_result = Workflow.check(workflow)
    if wf_result.get("status") != "success":
        errors.append({
            "node_id": "global",
            "message": wf_result.get("error_message", "Unknown workflow error"),
        })
    for node_id, node_dict in workflow.items():
        if node_id == "__meta__" or not isinstance(node_dict, dict):
            continue
        node_type = node_dict.get("node_type")
        node_cls = node_registry._module_dict.get(node_type)
        if node_cls is None:
            errors.append({"node_id": node_id, "message": f"Unknown node_type: {node_type}"})
            continue
        check_fn = getattr(node_cls, "check", None)
        if check_fn:
            cr = check_fn(node_dict)
            if isinstance(cr, dict) and cr.get("status") == "error":
                errors.append({"node_id": node_id, "message": cr.get("error_message", "")})
    return errors


def validate_workflow_model_names(
    workflow: dict,
    available_model_ids: set[str] | list[str] | tuple[str, ...],
) -> list[dict]:
    """Reject model-backed nodes whose public handle is not currently usable."""
    available = {str(name) for name in available_model_ids if str(name).strip()}
    errors: list[dict] = []
    for node_id, node in (workflow or {}).items():
        if node_id == "__meta__" or not isinstance(node, dict):
            continue
        if node.get("node_type") not in {"PromptNode", "SubAgentNode"}:
            continue
        model_name = str((node.get("node_config") or {}).get("model_name") or "").strip()
        if model_name in available:
            continue
        available_text = ", ".join(sorted(available)) if available else "none"
        errors.append({
            "node_id": node_id,
            "message": (
                f"Model {model_name!r} is not available for this user. "
                "Call get_config(scope='global') and copy one enabled models "
                f"key exactly. Available model handles: {available_text}."
            ),
        })
    return errors


async def validate_workflow_for_context(workflow: dict, ctx) -> list[dict]:
    """Structural/node validation plus tenant/user-scoped model validation."""
    from vibecanvas_api.services.platform_mcp.config_tools import (
        available_workflow_model_ids,
    )

    errors = validate_workflow(workflow)
    model_ids = await available_workflow_model_ids(ctx)
    errors.extend(validate_workflow_model_names(workflow, model_ids))
    return errors


def collect_workflow_warnings(workflow: dict) -> list[dict]:
    return Workflow.collect_warnings(workflow)


async def export_current_workflow(runtime: ToolRuntime, path: str) -> dict:
    ctx = runtime.context
    wf_id = target_workflow_id(ctx)
    workflow = ctx.repo.get_current_workflow(wf_id)
    if workflow is None:
        raise ToolError("no_workflow", "no workflow loaded")
    if not isinstance(workflow, dict):
        raise ToolError("bad_workflow", "workflow must be a JSON object")
    stamped = _stamp_meta(ctx, wf_id, workflow)
    await write_json_file(runtime, path, stamped)
    ctx.workflow = stamped
    return {
        "workflow": stamped,
        "workflow_id": wf_id,
        "path": path,
        "node_count": _node_count(stamped),
        "version": (stamped.get("__meta__") or {}).get("workflow_version", "?"),
        "subversion": (stamped.get("__meta__") or {}).get("workflow_subversion", "?"),
    }


def commit_workflow_file(ctx, workflow: dict, *, note: str) -> dict:
    wf_id = target_workflow_id(ctx)
    current = _stamp_meta(ctx, wf_id, workflow)
    tidied_nodes = _auto_tidy_workflow(current)
    ptr = ctx.repo.commit(wf_id, current, note=note)
    ctx.repo.mark_saved(wf_id)
    meta = current.setdefault("__meta__", {})
    version = meta.get("workflow_version", 1)
    meta["workflow_id"] = wf_id
    meta["workflow_version"] = version
    meta["workflow_subversion"] = ptr.sv
    ctx.workflow = current
    ctx.workflow_dirty = False
    ctx.pending_vibe = {
        "updates": [{"kind": "file_update_canvas", "note": note}],
        "apply_auto_layout": True,
        "new_workflow": current,
    }
    return {
        "workflow_id": wf_id,
        "version": version,
        "subversion": ptr.sv,
        "node_count": _node_count(current),
        "tidied_nodes": tidied_nodes,
        "workflow": current,
    }
