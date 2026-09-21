"""Platform MCP update_canvas tool."""

from __future__ import annotations

import asyncio

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.services.platform_mcp.build_tools.workflow_file import (
    DEFAULT_WORKFLOW_PATH,
    collect_workflow_warnings,
    commit_workflow_file,
    read_workflow_file,
    validate_workflow_for_context,
)
from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
)
from vibecanvas_api.services.platform_mcp.authorization import (
    recheck_platform_workflow_action,
)
from vibecanvas_api.services.platform_mcp.build_tools._target import (
    target_workflow_id,
)


def _validation_error_message(path: str, errors: list[dict]) -> str:
    lines = [
        "Canvas updated: no",
        "Status: blocked; the workflow has NOT been delivered to the user.",
        f"Canvas was not updated. {path} has {len(errors)} validation issue(s):"
    ]
    for e in errors[:20]:
        lines.append(f"- [{e.get('node_id', 'global')}] {e.get('message', '')}")
    if len(errors) > 20:
        lines.append(f"- ... {len(errors) - 20} more issue(s)")
    lines.append("Required next action: fix the workflow file, run check_workflow again, then retry update_canvas.")
    lines.append("Do not provide a final success answer while Canvas updated is no.")
    return "\n".join(lines)


def _blocked_error_message(path: str, reason: str) -> str:
    lines = [
        "Canvas updated: no",
        "Status: blocked; the workflow has NOT been delivered to the user.",
        f"Canvas was not updated because {path} could not be imported.",
        f"Reason: {reason}",
        "The current canvas is unchanged.",
        "Required next action: fix the workflow file, validate it with check_workflow, then retry update_canvas.",
        "Do not provide a final success answer while Canvas updated is no.",
    ]
    return "\n".join(lines)


def _bad_json_error_message(path: str, reason: str) -> str:
    lines = [
        "Canvas updated: no",
        "Status: blocked; the workflow has NOT been delivered to the user.",
        f"Canvas was not updated because {path} is not valid JSON.",
        f"Reason: {reason}",
        "The current canvas is unchanged.",
        "Fix the JSON syntax before checking or importing. JSON requires double quotes, lowercase true/false/null, and no trailing commas; Python dict syntax with single quotes is not valid JSON.",
        "For non-trivial workflow edits, prefer a short script that uses json.load to read the file, mutates the dict, then writes it with json.dump(..., ensure_ascii=False, indent=2).",
        f"Validate syntax with: python -m json.tool {path} >/tmp/workflow.valid.json",
        "Required next action: repair the file, run check_workflow again, then retry update_canvas.",
        "Do not provide a final success answer while Canvas updated is no.",
    ]
    return "\n".join(lines)


@register_render("update_canvas")
def _render_update_canvas(raw: dict, ctx) -> Rendered:
    errors = raw.get("errors") or []
    warnings = raw.get("warnings") or []
    path = raw.get("path") or DEFAULT_WORKFLOW_PATH
    if raw.get("status") == "blocked":
        content = _validation_error_message(path, errors)
        abstract = f"update_canvas → blocked by {len(errors)} validation error(s)"
    else:
        content = (
            "Canvas updated: yes\n"
            f"Canvas updated from {path}\n"
            f"Workflow ID: {raw.get('workflow_id')}\n"
            f"Version: v{raw.get('version')}.sv{raw.get('subversion')}\n"
            f"Nodes: {raw.get('node_count')}\n"
            f"Layout: tidied {raw.get('tidied_nodes', 0)} node(s)"
        )
        if warnings:
            lines = [content, f"Validation warnings: {len(warnings)}"]
            for w in warnings[:20]:
                lines.append(f"- [{w.get('node_id', 'global')}] {w.get('message', '')}")
            if len(warnings) > 20:
                lines.append(f"- ... {len(warnings) - 20} more warning(s)")
            content = "\n".join(lines)
        abstract = (
            f"update_canvas → v{raw.get('version')}.sv{raw.get('subversion')} "
            f"({raw.get('node_count')} nodes)"
        )
    return Rendered(
        content=content,
        content_type="text/plain",
        abstract=abstract,
        extras={"workflow_id": raw.get("workflow_id"), "path": path},
    )


@tool_output(content_type="text/plain", tool="update_canvas")
async def _do_update_canvas(
    workflow_path: str,
    runtime: ToolRuntime,
    require_valid: bool = True,
) -> dict:
    try:
        workflow = await read_workflow_file(runtime, workflow_path or DEFAULT_WORKFLOW_PATH)
        errors = await validate_workflow_for_context(workflow, runtime.context)
        warnings = collect_workflow_warnings(workflow)
        if errors and require_valid:
            path = workflow_path or DEFAULT_WORKFLOW_PATH
            raise ToolError(
                "validation_failed",
                _validation_error_message(path, errors),
                info={"path": path, "errors": errors},
            )

        await recheck_platform_workflow_action(
            runtime.context,
            target_workflow_id(runtime.context),
            Action.UPDATE,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        )

        def _commit() -> dict:
            return commit_workflow_file(
                runtime.context,
                workflow,
                note=f"agent: update_canvas from {workflow_path or DEFAULT_WORKFLOW_PATH}",
            )

        result = await asyncio.to_thread(_commit)
        result["path"] = workflow_path or DEFAULT_WORKFLOW_PATH
        result["errors"] = errors
        result["warnings"] = warnings
        result["status"] = "updated"
        return result
    except ToolError as exc:
        path = workflow_path or DEFAULT_WORKFLOW_PATH
        code = str(exc)
        if code == "bad_json":
            raise ToolError(
                code,
                _bad_json_error_message(path, exc.message or code),
                info={"path": path, "canvas_updated": False, "reason": exc.message or code},
            )
        if code in {"bad_json", "bad_workflow", "path_not_found", "invalid_path", "read_failed", "bad_file"}:
            raise ToolError(
                code,
                _blocked_error_message(path, exc.message or code),
                info={"path": path, "canvas_updated": False, "reason": exc.message or code},
            )
        raise
    except Exception as exc:
        path = workflow_path or DEFAULT_WORKFLOW_PATH
        raise ToolError(
            "update_failed",
            _blocked_error_message(path, str(exc)),
            info={"path": path, "canvas_updated": False, "reason": str(exc)},
        )


@tool(response_format="content_and_artifact")
async def update_canvas(
    workflow_path: str = DEFAULT_WORKFLOW_PATH,
    require_valid: bool = True,
    *,
    runtime: ToolRuntime,
) -> str:
    """Import a workflow JSON file into the current canvas.

    The file must contain a JSON object keyed by node ids, with optional
    `__meta__`. The tool validates graph structure and node configs. Keep
    `require_valid=True` for normal building and delivery; validation failures
    must be fixed before importing to the canvas. `require_valid=False` is only
    for explicit human-requested diagnostic imports of an invalid graph and does
    not count as a successful workflow delivery. On success, this tool applies a
    deterministic display layout and creates a new workflow subversion.

    Args:
        workflow_path: workflow JSON file to import.
        require_valid: leave true. Only set false when the user explicitly asks
            to inspect an invalid graph state; never use it to bypass validation
            during normal workflow construction or final delivery.

    Returns:
        content = update summary, new version/subversion, node count, or a
        "Canvas updated: no" failure explaining what to fix before retrying.
    """
    return await _do_update_canvas(workflow_path, runtime, require_valid)
