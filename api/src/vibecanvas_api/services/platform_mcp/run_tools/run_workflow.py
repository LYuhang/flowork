"""Platform MCP run_workflow tool — execute the current workflow.

The agent calls this tool on the host; the tool stages a workflow job under the
current agent session's workflow run dir and submits it to that same session's
resident gVisor job server. Agent shell/file/workflow execution therefore share
one sandbox and one mounted filesystem view.

Mirrors the frontend Execute button end-to-end:
  1. SAVE-BEFORE-RUN — if the workflow has unsaved edits, commit a subversion
     first (so the run executes the just-saved graph, never a stale HEAD). If the
     save fails the run is aborted, exactly like ``saveBeforeRun`` on the client.
  2. Agent tool execution does not write workflow-page run state. Workflow
     files/results always use the active workflow id as the stable `/run` scope.
"""
from __future__ import annotations

import asyncio
import json

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.services.platform_mcp.run_tools._backend import (
    _resolve_session_sync, _save_if_dirty,
)
from vibecanvas_api.agents.tools._session_fs import _resolve_session
from vibecanvas_api.services.node_results import build_node_payload, write_node_result_sync
from vibecanvas_api.services.workflow_sandbox_runner import (
    WorkflowSandboxRunError,
    run_workflow_once,
    run_workflow_once_sync,
)
from vibecanvas_api.agents.tools.decorator import tool_output, ToolError
from vibecanvas_api.agents.tools.render import register_render, Rendered
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
    ResourceType,
)
from vibecanvas_api.services.platform_mcp.authorization import (
    recheck_platform_workflow_action,
)
from vibecanvas_api.services.platform_mcp.build_tools._target import target_workflow_id
from vibecanvas_api.services.platform_mcp.build_tools.workflow_file import (
    read_text_file,
    read_workflow_file,
    write_json_file,
)

def _finalize_run_result(
    *,
    ctx,
    wf_id: str,
    workflow_run_id: str,
    workflow: dict,
    rj: dict,
    input_path: str,
    workflow_path: str,
    output_path: str,
) -> dict:
    previous_outputs = rj.get("final_outputs") or {}
    error_dict = rj.get("error_dict") or {}
    exec_time = rj.get("execution_time") or 0.0

    err_map = error_dict or {}
    name_to_id = {
        v.get("node_name"): nid
        for nid, v in workflow.items()
        if isinstance(v, dict) and v.get("node_name")
    }
    node_outputs: dict = {}
    for _name, _output in (previous_outputs or {}).items():
        try:
            json.dumps(_output)
            node_outputs[_name] = _output
        except (TypeError, ValueError):
            node_outputs[_name] = str(_output)
        node_id = name_to_id.get(_name)
        if not node_id:
            continue
        status = "error" if _name in err_map else "completed"
        err = str(err_map[_name]) if _name in err_map else None
        write_node_result_sync(workflow_run_id, build_node_payload(
            node_id=node_id, node_name=_name,
            node_type=(workflow.get(node_id) or {}).get("node_type"),
            status=status, inputs=None, output=_output, error=err))

    errors = {k: str(v) for k, v in err_map.items()} if err_map else {}
    return {
        "status": "success" if not errors else "partial_error",
        "node_outputs": node_outputs,
        "errors": errors,
        "execution_time": round(exec_time, 3),
        "input_path": input_path or None,
        "workflow_path": workflow_path or None,
        "output_path": output_path or None,
        "workflow_id": wf_id,
    }


@register_render("run_workflow")
def _render(raw: dict, ctx) -> Rendered:
    """run_workflow's presentation (co-located, §2.1)."""
    from vibecanvas_api.config import config
    n, errs = len(raw.get("node_outputs", {})), raw.get("errors", {})
    abstract = (f"Ran workflow in {raw.get('execution_time')}s — {raw.get('status')}, "
                f"{n} node outputs" + (f", {len(errs)} errors" if errs else "")
                + f". Per-node results saved under {config.vfs_paths.node_results_dir}/ "
                "(overwritten each run).")
    return Rendered(content=raw, content_type="application/json", abstract=abstract,
                    extras={"workflow_id": raw.get("workflow_id")})


@tool_output(content_type="application/json", tool="run_workflow")
def _sync_run_workflow(
    inputs: str,
    runtime: ToolRuntime,
    parsed_inputs: dict | None = None,
    workflow_override: dict | None = None,
    output_path: str = "",
    input_path: str = "",
    workflow_path: str = "",
):
    """Worker (sync, run via to_thread): returns the raw run-result dict or raises
    ToolError. Still callable directly (tests / the @tool wrapper)."""
    try:
        ctx = runtime.context
        wf_id = target_workflow_id(ctx)
        workflow = workflow_override or ctx.workflow
        run_inputs = parsed_inputs
        if run_inputs is None:
            try:
                run_inputs = json.loads(inputs) if isinstance(inputs, str) else inputs
            except (TypeError, ValueError):
                raise ToolError(
                    "bad_inputs",
                    "inputs must be a JSON object string; use node_execute for single-node runs.",
                )
        if run_inputs is None:
            run_inputs = {}
        if not isinstance(run_inputs, dict):
            raise ToolError("bad_inputs", "workflow inputs must be a JSON object")

        save_err = _save_if_dirty(ctx)
        if save_err is not None:
            raise ToolError("save_before_run_failed", save_err)

        session = _resolve_session_sync(ctx)
        if session is None:
            raise ToolError("no_workspace",
                            "no workspace is available for this workflow — running a "
                            "workflow requires the workflow workspace")
        workflow_run_id = wf_id
        tenant_id = getattr(ctx, "tenant_id", None) or ""
        if not tenant_id:
            raise ToolError("no_workspace", "tenant context is required to run a workflow")
        try:
            job = run_workflow_once_sync(
                session,
                tenant_id=tenant_id,
                workflow=workflow,
                inputs=run_inputs,
                workflow_run_id=workflow_run_id,
                user_id=str(getattr(ctx, "username", "") or ""),
                workflow_id=wf_id,
                execution_id=str(getattr(ctx, "turn_id", "") or wf_id),
                execution_resource_type=ResourceType.AGENT_RUN.value,
                clear_run=True,
                install_dependencies=True,
            )
            rj = job.result_json
        except WorkflowSandboxRunError:
            raise ToolError("run_failed", "the workflow run failed")
        except Exception:
            # Clean agent-facing message; internal sandbox details stay out of
            # the tool result.
            raise ToolError("run_failed", "the workflow run failed")
        try:
            return _finalize_run_result(
                ctx=ctx,
                wf_id=wf_id,
                workflow_run_id=workflow_run_id,
                workflow=workflow,
                rj=rj,
                input_path=input_path,
                workflow_path=workflow_path,
                output_path=output_path,
            )
        except Exception:
            raise ToolError("run_failed", "the workflow run failed")
    except ToolError:
        raise                                    # explicit agent-facing errors propagate as-is
    except Exception:                            # truly-unexpected → run_failed (no internal leak)
        raise ToolError("run_failed", "the workflow run failed")


@tool_output(content_type="application/json", tool="run_workflow")
async def _async_run_workflow(
    inputs: str,
    runtime: ToolRuntime,
    parsed_inputs: dict | None = None,
    workflow_override: dict | None = None,
    output_path: str = "",
    input_path: str = "",
    workflow_path: str = "",
):
    """Production worker: run on the agent event loop and reuse the same
    stream-capable workflow sandbox runner as the workflow page."""
    try:
        ctx = runtime.context
        wf_id = target_workflow_id(ctx)
        workflow = workflow_override or ctx.workflow
        run_inputs = parsed_inputs
        if run_inputs is None:
            try:
                run_inputs = json.loads(inputs) if isinstance(inputs, str) else inputs
            except (TypeError, ValueError):
                raise ToolError("bad_inputs", "inputs must be a JSON object string")
        if run_inputs is None:
            run_inputs = {}
        if not isinstance(run_inputs, dict):
            raise ToolError("bad_inputs", "workflow inputs must be a JSON object")

        save_err = await asyncio.to_thread(_save_if_dirty, ctx)
        if save_err is not None:
            raise ToolError("save_before_run_failed", save_err)

        session = await _resolve_session(ctx)
        if session is None:
            raise ToolError(
                "no_workspace",
                "no workspace is available for this workflow — running a workflow "
                "requires the workflow workspace",
            )
        workflow_run_id = wf_id
        tenant_id = getattr(ctx, "tenant_id", None) or ""
        if not tenant_id:
            raise ToolError("no_workspace", "tenant context is required to run a workflow")

        try:
            job = await run_workflow_once(
                session,
                tenant_id=tenant_id,
                workflow=workflow,
                inputs=run_inputs,
                workflow_run_id=workflow_run_id,
                user_id=str(getattr(ctx, "username", "") or ""),
                workflow_id=wf_id,
                execution_id=str(getattr(ctx, "turn_id", "") or wf_id),
                execution_resource_type=ResourceType.AGENT_RUN.value,
                clear_run=True,
                install_dependencies=True,
            )
        except WorkflowSandboxRunError:
            raise ToolError("run_failed", "the workflow run failed")

        return await asyncio.to_thread(
            _finalize_run_result,
            ctx=ctx,
            wf_id=wf_id,
            workflow_run_id=workflow_run_id,
            workflow=workflow,
            rj=job.result_json,
            input_path=input_path,
            workflow_path=workflow_path,
            output_path=output_path,
        )
    except ToolError:
        raise
    except Exception:
        raise ToolError("run_failed", "the workflow run failed")


@tool(response_format="content_and_artifact")
async def run_workflow(
    input_path: str = "",
    output_path: str = "/run/workflow.result.json",
    workflow_path: str = "",
    inputs: str = "{}",
    *,
    runtime: ToolRuntime,
) -> str:
    """Execute a workflow and write the run result to a JSON file.

    `input_path`, when provided, must contain a JSON object with workflow-level
    inputs. If `input_path` is empty, `inputs` may contain an inline JSON object
    string. If `workflow_path` is provided, that workflow JSON file is executed
    instead of the current canvas workflow. The result is also written to
    `output_path` as JSON.

    Args:
        input_path: optional JSON object file for workflow inputs.
        output_path: JSON file path for the run result. Default: /run/workflow.result.json.
        workflow_path: optional workflow JSON file to run instead of the current canvas.
        inputs: compatibility inline JSON object string when input_path is empty.

    Returns:
        content = {status,node_outputs,errors,execution_time,input_path,workflow_path,workflow_id,output_path}.
    """
    parsed_inputs = None
    workflow_override = None
    if input_path:
        try:
            parsed_inputs = json.loads(await read_text_file(runtime, input_path))
        except ToolError:
            raise
        except (TypeError, ValueError) as exc:
            raise ToolError("bad_inputs", f"input_path {input_path!r} is not valid JSON: {exc}")
    if workflow_path:
        workflow_override = await read_workflow_file(runtime, workflow_path)
    await recheck_platform_workflow_action(
        runtime.context,
        target_workflow_id(runtime.context),
        Action.EXECUTE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    content, artifact = await _async_run_workflow(
        inputs,
        runtime,
        parsed_inputs,
        workflow_override,
        output_path,
        input_path,
        workflow_path,
    )
    if output_path and artifact.get("status") == "success":
        try:
            await write_json_file(runtime, output_path, json.loads(artifact.get("content") or content))
        except Exception:
            # The run succeeded; preserve the tool result even if writing the
            # optional convenience result file fails.
            pass
    return content, artifact
