"""Platform MCP node_execute tool — run one node in the current sandbox.

Mirrors the frontend node-debug path: unsaved edits are committed first
(save-before-run), then a node job is staged under the current agent session's
workflow run dir and submitted to that same session's resident gVisor job server.
The terminal frame is persisted into the workflow's fixed run-tier
(``/run/__exec__/nodes/{id}.json``).
"""
from __future__ import annotations

import asyncio
import json

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_engine.node import node_registry
from vibecanvas_api.services.platform_mcp.run_tools._backend import (
    _save_if_dirty,
)
from vibecanvas_api.services.node_results import (
    node_result_path,
    persist_node_frame_payload,
    write_node_result,
)
from vibecanvas_api.services.workflow_sandbox_runner import (
    WorkflowSandboxRunError,
    run_node_once,
)
from vibecanvas_api.agents.tools.decorator import tool_output, ToolError
from vibecanvas_api.agents.tools.render import register_render, Rendered
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
)
from vibecanvas_api.services.platform_mcp.authorization import (
    recheck_platform_workflow_action,
)
from vibecanvas_api.agents.tools._session_fs import _resolve_session
from vibecanvas_api.services.platform_mcp.build_tools._target import target_workflow_id
from vibecanvas_api.services.platform_mcp.build_tools.workflow_file import (
    read_text_file,
    write_json_file,
)


@register_render("node_execute")
def _render(raw: dict, ctx) -> Rendered:
    """node_execute's presentation (co-located, §2.1): a domain abstract, the node_id
    chaining handle, and the persisted /run result path. `raw` = the node-result dict
    (node_id/status already in it, so the agent reads them from content)."""
    node_id = raw.get("node_id")
    ok = raw.get("status") == "success"
    try:                                           # defensive: a bad id must not fail the run
        path = node_result_path(node_id) if node_id else None
    except ValueError:
        path = None
    abstract = (f"Ran node {node_id} ({raw.get('node_type')}) — "
                + ("success" if ok else f"error: {raw.get('error')}"))
    if path:
        abstract += f". Result saved at {path} (overwritten on each run)"
    return Rendered(content=raw, content_type="application/json", abstract=abstract,
                    path=path, extras={"node_id": node_id, "workflow_id": raw.get("workflow_id")})


@tool_output(content_type="application/json", tool="node_execute")
async def _node_execute(
    node: str,
    inputs: str,
    runtime: ToolRuntime,
    input_path: str = "",
    output_path: str = "",
):
    """Worker: returns the raw node-result dict (the render builds the envelope) or
    raises ToolError. Still callable directly (tests / the @tool wrapper)."""
    ctx = runtime.context
    wf_id = target_workflow_id(ctx)
    node_dict = ctx.workflow.get(node)
    if not node_dict or not isinstance(node_dict, dict):
        raise ToolError("unknown_node", f"node {node!r} not found — pass a valid node_id.")
    if input_path:
        try:
            parsed_inputs = json.loads(await read_text_file(runtime, input_path))
        except ToolError:
            raise
        except (TypeError, ValueError) as e:
            raise ToolError("bad_inputs", f"input_path {input_path!r} is not valid JSON: {e}")
    else:
        try:
            parsed_inputs = json.loads(inputs) if (inputs or "").strip() else {}
        except (TypeError, ValueError) as e:
            raise ToolError("bad_inputs", f"inputs is not valid JSON: {e}")
    if not isinstance(parsed_inputs, dict):
        raise ToolError("bad_inputs", "node inputs must be a JSON object")
    node_type = node_dict.get("node_type")
    if node_registry._module_dict.get(node_type) is None:
        raise ToolError("unknown_node_type", f"unknown node_type: {node_type}")

    await recheck_platform_workflow_action(
        ctx,
        wf_id,
        Action.EXECUTE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    save_err = await asyncio.to_thread(_save_if_dirty, ctx)
    if save_err is not None:
        raise ToolError("save_before_run_failed", save_err)

    session = await _resolve_session(ctx)
    if session is None:
        raise ToolError("no_workspace",
                        "no workspace is available for this workflow — running a "
                        "node requires the workflow workspace")
    tenant_id = getattr(ctx, "tenant_id", None)
    nid = node_dict.get("node_id") or node
    workflow_run_id = wf_id
    if not tenant_id:
        raise ToolError("no_workspace", "tenant context is required to run a node")
    try:
        job = await run_node_once(
            session,
            tenant_id=tenant_id,
            node=node_dict,
            inputs=parsed_inputs,
            workflow_run_id=workflow_run_id,
            workflow=ctx.workflow,
            clear_run=False,
            install_dependencies=True,
        )
        rj = job.result_json
    except WorkflowSandboxRunError:
        raise ToolError("run_failed", f"could not run node {node!r}")
    except Exception:                            # provider/sandbox failure — don't leak internals
        raise ToolError("run_failed", f"could not run node {node!r}")

    error_dict = rj.get("error_dict") or {}
    final_outputs = rj.get("final_outputs") or {}
    if "__engine__" in error_dict:               # in-sandbox engine crash — clean message, no leak
        terminal = {"status": "error", "error": "node execution failed"}
    elif error_dict.get(nid):
        # NODE-level error (the node's own message, e.g. "[Prompt]: missing input")
        # — this IS for the agent; keep it.
        terminal = {"status": "error", "error": str(error_dict[nid])}
    else:
        terminal = {"status": "completed",
                    "result": json.dumps(final_outputs.get(nid),
                                         default=str, ensure_ascii=False)}

    # Persist into the workflow's fixed run-tier (overwrite this one node's file).
    if tenant_id:
        payload = persist_node_frame_payload(
            {**terminal, "node_id": node, "node_type": node_type})
        if payload is not None:
            await write_node_result(workflow_run_id, tenant_id, payload)

    is_success = terminal.get("status") == "completed"
    output = json.loads(terminal["result"]) if (is_success and "result" in terminal) else None
    # Return the raw node-result dict; the registered render (_render) lays it out +
    # builds the v2 envelope. A node that errored is still a SUCCESSFUL tool call
    # whose content.status == "error" (the agent inspects it).
    result = {"status": "success" if is_success else "error",
              "node_id": node, "node_type": node_type,
              "input_path": input_path or None,
              "output": output if is_success else None,
              "error": terminal.get("error") if not is_success else None,
              "workflow_id": wf_id}
    if output_path:
        await write_json_file(runtime, output_path, result)
        result["output_path"] = output_path
    return result


@tool(response_format="content_and_artifact")
async def node_execute(
    node: str,
    input_path: str = "",
    output_path: str = "",
    inputs: str = "",
    *,
    runtime: ToolRuntime,
) -> str:
    """Run one node and optionally read/write JSON files for inputs/results.

    `input_path`, when provided, must contain a JSON object. If `input_path` is
    empty, `inputs` may contain an inline JSON object string. A node-level
    failure is returned as a normal result with `status:"error"`.

    Args:
        node: the node_id to run (e.g. "node_2").
        input_path: optional JSON object file for node inputs.
        output_path: optional JSON file path for the result.
        inputs: compatibility inline JSON object string when input_path is empty.

    Returns:
        content = {status,node_id,node_type,input_path,output,error,workflow_id,output_path}.
    """
    return await _node_execute(node, inputs, runtime, input_path, output_path)
