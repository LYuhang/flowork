# -*- coding: utf-8 -*-
# Platform MCP batch_execute tool.
"""Run the current workflow once per input-table row.

Rows may execute concurrently, but the tool call is synchronous: it returns
only after the output file has been written.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
)
from vibecanvas_api.services.platform_mcp.authorization import (
    recheck_platform_workflow_action,
)
from vibecanvas_api.agents.tools._session_fs import _require_session
from vibecanvas_api.services.platform_mcp.build_tools._target import target_workflow_id
from vibecanvas_api.services.platform_mcp.run_tools.table_io import _read_rows
from vibecanvas_api.services.platform_mcp.run_tools._batch_coro import _batch_coro_rows


def _output_path(input_path: str) -> str:
    p = Path(input_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    parent = str(p.parent).replace("\\", "/")
    if parent == ".":
        parent = "/run/batch"
    if not parent.startswith("/run"):
        parent = "/run/batch"
    return f"{parent.rstrip('/')}/{p.stem}_batch_results_{ts}.jsonl"


@register_render("batch_execute")
def _render(raw: dict, ctx) -> Rendered:
    content = (
        f'Batch completed: "{raw.get("name", "")}"\n'
        f'{raw.get("total_rows")} rows | {raw.get("failed_rows")} failed | '
        f'output → {raw.get("output_path")}\n'
        f'status: {raw.get("status")}')
    abstract = (
        f'Batch completed → {raw.get("output_path")} '
        f'({raw.get("total_rows")} rows, {raw.get("failed_rows")} failed)'
    )
    return Rendered(content=content, content_type="application/json", abstract=abstract,
                    path=raw.get("output_path"),
                    extras={"workflow_id": raw.get("workflow_id")})


@tool_output(content_type="application/json", tool="batch_execute")
async def _do_batch_execute(input_path: str, name: str, row_concurrency: int,
                            runtime: ToolRuntime, sheet: str = "",
                            output_path: str = "") -> dict:
    ctx = runtime.context
    wf_id = target_workflow_id(ctx)
    try:
        session = await _require_session(ctx)
        rows, columns = await _read_rows(input_path, session, sheet)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("read_error", f"cannot read input table: {exc}")
    if not rows:
        raise ToolError("empty_table", "input table has no data rows")

    resolved_output_path = output_path or _output_path(input_path)
    resolved_name = name or Path(input_path).name
    await recheck_platform_workflow_action(
        ctx,
        wf_id,
        Action.EXECUTE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    result = await _batch_coro_rows(
        rows,
        resolved_output_path,
        ctx.workflow,
        ctx,
        row_concurrency,
        session,
    )
    failed_rows = int(result.get("failed_rows") or 0)
    return {
        "name": resolved_name,
        "input_path": input_path,
        "sheet": sheet or None,
        "columns": columns,
        "output_path": resolved_output_path,
        "output_format": "jsonl",
        "total_rows": len(rows),
        "failed_rows": failed_rows,
        "status": "completed" if failed_rows == 0 else "completed_with_errors",
        "workflow_id": wf_id,
        "result_summary": result.get("result_summary"),
        "hint": "Results were written as JSON Lines to output_path.",
    }


@tool(response_format="content_and_artifact")
async def batch_execute(input_path: str, name: str = "", row_concurrency: int = 4,
                        sheet: str = "", output_path: str = "",
                        *, runtime: ToolRuntime) -> str:
    """Run the current workflow once per row of an input table.

    Supported input formats are `.csv`, `.tsv`, `.jsonl`, `.json`, `.xlsx`, and
    `.xlsm`. For `.xlsx/.xlsm`, provide `sheet` when the workbook has multiple
    sheets. Rows execute concurrently up to `row_concurrency`, while this tool
    call waits for the full batch to finish. Each input row becomes one
    workflow-level input JSON object. Results are written as JSON Lines with fields
    `{index,status,input,output,node_outputs,errors,execution_time}`.

    Args:
        input_path: input table path.
        name: a label for this batch (defaults to the file name).
        row_concurrency: how many rows to run in parallel (default 4).
        sheet: worksheet name for xlsx/xlsm inputs.
        output_path: optional JSONL output path.

    Returns:
        The completed batch summary and output path.
    """
    return await _do_batch_execute(input_path, name, row_concurrency, runtime, sheet, output_path)
