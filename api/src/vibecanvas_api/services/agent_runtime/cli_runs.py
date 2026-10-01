"""Turn-owned execution jobs. Private polling keeps the Runtime bus responsive.

The registry is deliberately ephemeral, not a resumable task service. A live
CLI must renew its lease; loss of its turn/socket cancels outstanding work.
Only acknowledged events are discarded, with bounded producer backpressure.
"""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time
import json
from time import monotonic

import structlog

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.agents.tools._session_fs import _require_session
from vibecanvas_api.authorization.types import Action, ConsistencyPreference, ResourceType
from vibecanvas_api.flowork_cli.cli import error
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.authorization import (
    require_workflow_action,
)
from vibecanvas_api.services.agent_resources.workflow_graph import validate_workflow_for_context
from vibecanvas_api.services.agent_resources.batch_results import _jsonl_record
from vibecanvas_api.services.agent_resources.table_io import _text_to_rows, _xlsx_to_rows
from vibecanvas_api.services.llm_credentials_inject import inject_into_run_context_async
from vibecanvas_api.services.workflow_sandbox_runner import prepare_code_pythonpath
from vibecanvas_api.services.agent_resources.node_execution import select_execution_node
from . import cli_run_lease
from vibecanvas_api.services.agent_resources.workflow_transfer import read_workflow_snapshot
from vibecanvas_api.services.workflow_execution_history import create_execution, observe_execution

logger = structlog.get_logger(__name__)
LEASE_SECONDS = 20.0


def _scope(capability) -> tuple:
    return (capability.tenant_id, capability.user_id, capability.chat_id,
            capability.turn_id, capability.runtime_session_id)


def _json_cell(value):
    if isinstance(value, (date, datetime, datetime_time)):
        return value.isoformat()
    raise ValueError("Unsupported spreadsheet cell value.")


def _batch_rows(arguments: dict) -> list[dict]:
    try:
        data = base64.b64decode(arguments["data"], validate=True)
        ext = arguments["format"]
        if ext in {"xlsx", "xlsm"}:
            rows, _ = _xlsx_to_rows(data, arguments["name"], arguments["sheet"])
        else:
            parsed = _text_to_rows(data.decode("utf-8-sig"), ext)
            if parsed is None:
                raise ValueError("not tabular")
            rows, _ = parsed
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows must be objects")
        # Explicitly normalize workbook date cells; reject NaN/infinity, invalid
        # CSV columns and other non-JSON values before any execution is started.
        if any(any(not isinstance(key, str) for key in row) for row in rows):
            raise ValueError("invalid column")
        return json.loads(json.dumps(rows, default=_json_cell, allow_nan=False))
    except ToolError:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError) as exc:
        raise ToolError("invalid_input", "Batch input must contain a valid table of JSON objects.") from exc


@dataclass
class Run:
    capability: object
    run_id: str
    operation: str
    queue: asyncio.Queue
    lease: float = field(default_factory=monotonic)
    task: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    sequence: int = 0
    pending: dict | None = None
    workflow_id: str = ""
    reference: dict = field(default_factory=dict)
    session: object = None
    active: dict = field(default_factory=dict)
    total: int = 0
    completed: int = 0
    failed: int = 0
    stopping: bool = False
    cancellation_confirmed: bool = True
    pool_used: bool = False
    pool_close_task: asyncio.Task | None = None
    durable_lease: bool = False
    capacity: int = 1

    async def emit(self, **event):
        await self.queue.put({"run_id": self.run_id, **self.reference, **event})


_runs: dict[tuple, Run] = {}


async def _authorize(run: Run):
    ctx = await agent_context.resolve_context(run.capability)
    if run.workflow_id:
        await require_workflow_action(ctx, run.workflow_id, Action.EXECUTE,
                                      consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    return ctx


async def _close_pool(run: Run) -> bool:
    """Exactly one close request per CLI execution, shared by all row workers."""
    run.stopping = True
    if not run.pool_used:
        return True
    if run.pool_close_task is None:
        run.pool_close_task = asyncio.create_task(run.session.close_workflow_pool(
            tenant=run.capability.tenant_id, pool_id=run.run_id, history=True,
        ))
    try:
        result = await asyncio.wait_for(asyncio.shield(run.pool_close_task), timeout=20)
        if not isinstance(result, dict) or result.get("closed") is not True:
            raise RuntimeError("Execution pool shutdown was not confirmed")
        return True
    except (Exception, asyncio.CancelledError):
        run.cancellation_confirmed = False
        logger.warning("workflow_cli_pool_close_unconfirmed", run_id=run.run_id)
        return False


async def _execute_one(run: Run, workflow: dict, row: dict, index: int, code_pythonpath, resources=None):
    ctx = await _authorize(run)
    injected = await inject_into_run_context_async(
        {}, workflow, ctx.tenant_id, user_id=ctx.username, workflow_id=run.workflow_id,
        execution_id=ctx.turn_id, execution_resource_type=ResourceType.AGENT_RUN.value,
    )
    extra = dict(injected)
    if resources:
        extra["workflow_resources"] = resources
    if code_pythonpath:
        extra["code_pythonpath"] = code_pythonpath
    if run.stopping:
        raise asyncio.CancelledError()
    node_id = run.reference.get("node_id")
    job_id = await create_execution(
        tenant_id=ctx.tenant_id, source_type="workflow", source_id=run.workflow_id,
        user_id=ctx.username, workflow_id=run.workflow_id, workflow=workflow, inputs=row, node_id=node_id,
        input_index=index if run.operation == "workflow.run-batch" else None,
    )
    subpath = f"cli/{run.run_id}/{index}"
    run.active[job_id] = subpath
    run.pool_used = True
    link = {"execution_id": job_id, "execution_url": f"/workflow-executions/{job_id}"}

    async def on_state(state):
        await run.emit(status="running", row_status=state, index=index, **link,
                       total=run.total, completed=run.completed, failed=run.failed)

    execution = asyncio.create_task(
        observe_execution(
            tenant_id=ctx.tenant_id, execution_id=job_id, on_state=on_state, on_failure=lambda: _close_pool(run),
            execute=run.session.execute_workflow_job(
                workflow=workflow, inputs=row, extra=extra or None, tenant=ctx.tenant_id,
                run_id=job_id, run_subpath=subpath, execution_pool_id=run.run_id,
                history_id=job_id, execution_capacity=run.capacity,
                **({"node_id": node_id} if node_id else {}),
            ),
        )
    )
    try:
        # Keep the RPC alive until pool shutdown is acknowledged. Cancelling
        # an RPC alone is not proof that its sandbox process has exited.
        response = await asyncio.shield(execution)
        status = response.get("status") or {}
        if isinstance(status, dict) and (status.get("ok") is False or status.get("status") in {"cancelled", "error", "timeout"}):
            raise ToolError("execution_failed", "The sandbox job failed or was cancelled; inspect partial output and side effects before retrying.")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ToolError("execution_failed", "The sandbox returned no workflow result; inspect side effects before retrying.")
        record = _jsonl_record(index, row, result)
        record.update(link)
        if node_id:
            # The node runner keys final_outputs by ID, unlike the graph's __end__.
            record["output"] = record["node_outputs"].get(node_id)
            if not record["errors"] and node_id not in record["node_outputs"]:
                raise ToolError("execution_failed", "The sandbox returned no result for the selected node.")
        return record
    except BaseException:
        await _close_pool(run)
        raise
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        run.active.pop(job_id, None)


async def _work(run: Run, arguments: dict):
    workers = []
    try:
        rows = await asyncio.to_thread(_batch_rows, arguments) if run.operation == "workflow.run-batch" else [arguments["inputs"]]
        ctx = await _authorize(run)
        snapshot = await read_workflow_snapshot(ctx, workflow_id=arguments["workflow_id"], major=arguments["major"])
        run.workflow_id = snapshot["id"]
        await _authorize(run)
        workflow = deepcopy(arguments.get("workflow", snapshot["workflow"]))
        run.reference = {"id": run.workflow_id, "version": snapshot["version"],
                         "source": "file" if "workflow" in arguments else "saved"}
        await cli_run_lease.reserve(ctx, run.run_id, run.workflow_id)
        run.durable_lease = True
        if "node" in arguments:
            run.reference["node_id"] = arguments["node"]
            workflow = await select_execution_node(workflow, arguments["node"], ctx)
            run.reference["node_type"] = workflow[arguments["node"]]["node_type"]
            errors = []
        else:
            errors = await validate_workflow_for_context(workflow, ctx)
        if errors:
            await run.emit(status="failed", **error("invalid_workflow", "Workflow validation failed.", "Run workflow check and repair the graph."), errors=errors, terminal=True, exit_code=1)
            return
        run.session = await _require_session(ctx)
        run.total = len(rows)
        run.capacity = max(1, min(arguments.get("concurrency", 1), len(rows), 16))
        if run.operation == "workflow.run-batch":
            run.reference["name"] = arguments["name"]
        await run.emit(status="running", total=run.total, completed=0, failed=0)
        code_pythonpath = await prepare_code_pythonpath(workflow, session=run.session)
        from vibecanvas_api.services.workflow_resources import prepare_execution_resources
        resources = await prepare_execution_resources(
            sandbox_session=run.session, workflow=workflow, tenant_id=ctx.tenant_id,
            user_id=ctx.username, workflow_id=run.workflow_id, execution_id=ctx.turn_id,
            execution_resource_type=ResourceType.AGENT_RUN.value,
        )
        iterator = iter(enumerate(rows))

        async def worker():
            for index, row in iterator:
                if run.stopping:
                    return
                record = await _execute_one(run, workflow, row, index, code_pythonpath, resources)
                run.completed += 1
                run.failed += int(record["status"] == "error")
                if run.operation == "workflow.run-batch":
                    await run.emit(status="running", total=run.total, completed=run.completed,
                                   failed=run.failed, index=index, record=record,
                                   execution_id=record["execution_id"], execution_url=record["execution_url"])
                else:
                    await run.emit(status="running", total=1, completed=1, failed=run.failed,
                                   execution_id=record["execution_id"], execution_url=record["execution_url"],
                                   result={"run_id": run.run_id, **run.reference, **record},
                                   **({"errors": record["errors"], "message": "The selected node failed. Inspect errors and the result file before retrying."}
                                      if run.reference.get("node_id") and record["errors"] else {}))

        for _ in range(min(run.capacity, len(rows))):
            workers.append(asyncio.create_task(worker()))
        await asyncio.gather(*workers)
        if not await _close_pool(run):
            raise ToolError("pool_close_unconfirmed", "The execution finished, but its worker pool shutdown could not be confirmed.")
        await run.session.writeback_vfs()
        await run.emit(status="completed_with_errors" if run.failed else "completed", total=run.total,
                       completed=run.completed, failed=run.failed, terminal=True, exit_code=1 if run.failed else 0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Stop siblings before publishing a terminal failure. Do not treat a
        # platform/auth failure as an ordinary per-row node error.
        run.stopping = True
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await _close_pool(run)
        if isinstance(exc, ToolError):
            failure = error(str(exc), exc.message or str(exc), "Inspect partial results and side effects before deciding whether to retry.")
        else:
            logger.exception("workflow_cli_execution_failed", run_id=run.run_id)
            failure = error("execution_failed", "Workflow execution stopped because a platform operation failed.", "Keep partial results; inspect side effects before retrying.")
        await run.emit(status="failed", **failure, total=run.total, completed=run.completed,
                       failed=run.failed, terminal=True,
                       exit_code=2 if str(exc) in {"invalid_input", "sheet_required", "sheet_not_found", "read_failed"} else 1)
    finally:
        run.stopping = True
        for worker_task in workers:
            if not worker_task.done():
                worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await _close_pool(run)
        if run.durable_lease and run.cancellation_confirmed:
            await cli_run_lease.release(run.capability.tenant_id, run.run_id)
            run.durable_lease = False


async def _stop(key: tuple):
    run = _runs.pop(key, None)
    if run is None:
        return None
    run.stopping = True
    if run.watchdog is not asyncio.current_task():
        run.watchdog.cancel()
    run.task.cancel()
    await asyncio.gather(run.task, return_exceptions=True)
    return run.cancellation_confirmed


async def _watch(key: tuple, run: Run):
    while key in _runs:
        await asyncio.sleep(2)
        if monotonic() - run.lease > LEASE_SECONDS:
            await _stop(key)
            return


async def cancel_turn_runs(tenant_id: str, chat_id: str, turn_id: str):
    keys = [key for key in _runs if key[0] == tenant_id and key[2:4] == (chat_id, turn_id)]
    await asyncio.gather(*(_stop(key) for key in keys))


async def command(capability, operation: str, arguments: dict) -> dict:
    key = (*_scope(capability), arguments["run_id"])
    if operation == "workflow.run.cancel":
        # Ownership comes from the signed original capability, not a new turn.
        # Cancellation remains possible after execution permission was revoked.
        confirmed = await _stop(key)
        return {"status": "not_running" if confirmed is None else "cancelled" if confirmed else "cancel_requested",
                "run_id": arguments["run_id"],
                "message": "No execution is attached to this turn." if confirmed is None else
                "Execution has stopped." if confirmed else "Cancellation was requested, but execution exit could not be confirmed. Inspect side effects before retrying."}
    if operation in {"workflow.run", "workflow.run-batch"}:
        if key in _runs:
            return error("run_already_exists", "This execution ID already exists.", "Do not repeat the start request; inspect its progress.")
        run = Run(capability, arguments["run_id"], operation,
                  asyncio.Queue(maxsize=max(8, 2 * arguments.get("concurrency", 1))))
        _runs[key] = run
        run.task = asyncio.create_task(_work(run, arguments))
        run.watchdog = asyncio.create_task(_watch(key, run))
        return {"status": "preparing", "run_id": run.run_id}
    run = _runs.get(key)
    if run is None:
        return error("result_unknown", "The execution is no longer attached to this turn.", "Inspect partial output and side effects; do not automatically rerun.")
    try:
        await _authorize(run)
    except BaseException:
        await _stop(key)
        raise
    ack = arguments["ack"]
    if ack > run.sequence or ack < run.sequence - int(run.pending is not None):
        return error("invalid_ack", "Invalid execution progress acknowledgement.", "Do not reuse an execution connection.")
    if run.pending is not None and ack == run.sequence:
        run.pending = None
    run.lease = monotonic()
    if run.durable_lease:
        renewed = await cli_run_lease.renew(capability.tenant_id, run.run_id)
        if not renewed:
            await _stop(key)
            return error("result_unknown", "The execution ownership lease expired.", "Inspect partial output before retrying.")
    if run.pending is None:
        try:
            run.pending = run.queue.get_nowait()
            run.sequence += 1
        except asyncio.QueueEmpty:
            pass
    return {"sequence": run.sequence, "event": run.pending}
