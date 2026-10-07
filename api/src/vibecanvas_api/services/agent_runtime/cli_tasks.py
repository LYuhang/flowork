"""Task CLI adapter over the same authorized operations as Task Center."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from datetime import date, datetime, time as datetime_time
import io
import json
import uuid

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import text
import structlog

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.types import Action
from vibecanvas_api.flowork_cli.cli import error, uncertain_result
from vibecanvas_api.flowork_cli.task_cli import FIXED_COLUMNS, WRITE_OPERATIONS
from vibecanvas_api.routes import tasks as routes, workflows
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.authorization import _require_active_chat_write, require_workflow_action
from vibecanvas_api.services.agent_runtime.resource_routes import resource_route_params, admitted_resource_route_params, visible_resource_rows
from vibecanvas_api.services.agent_resources.workflow_transfer import read_workflow_snapshot
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_tasks import TasksRepo

logger = structlog.get_logger(__name__)
TERMINAL = {"finished", "finished_with_errors", "failed", "interrupted", "cancelled"}


def _feedback(value, task_id, *, execution=None, plan=False):
    """Explain business state, never confuse command success with execution success."""
    value = dict(value)
    value.pop("id", None)
    value["task_id"] = str(task_id)
    task_type = value.get("task_type") or ("schedule_run" if plan or execution or value.get("execution") else "batch_exec")
    value["task_type"] = task_type
    if "schedule" in value:
        value["schedule"] = _schedule(value["schedule"])
    if value.get("execution"):
        value["execution"] = _execution(value["execution"], task_id)
    target = f"--task-id {task_id} --task-type {task_type}"
    get = f"flowork-cli task status {target}"
    if execution:
        value["execution_id"] = str(execution)
        target += f" --execution-id {execution}"
        get = f"flowork-cli task status {target}"
    logs = f"flowork-cli task logs {target}"
    follow = logs + " --follow"
    status = value.get("status")
    result = value.get("result") or {}
    if plan:
        enabled = (value.get("schedule") or {}).get("enabled")
        value["status"] = "enabled" if enabled else "paused"
        value["message"] = (
            "Schedule is enabled for future dispatch; this is not an execution result."
            if enabled else "Schedule is paused for future dispatch; existing executions are not cancelled."
        )
        value["hint"] = f"flowork-cli task history {target}"
    elif result.get("outcome_unknown"):
        value["message"] = "Execution outcome is unknown. External side effects may already have occurred. Do not automatically resume or resubmit."
        value["hint"] = logs
    elif status in {"queued", "resuming", "running"}:
        value["message"] = {
            "queued": "Task submitted. Execution continues in the background; results are not ready yet. Do not submit again.",
            "resuming": "Resume submitted for the same Task ID. Execution continues in the background; results are not ready yet. Do not resume again.",
            "running": "Execution is still running in the background; this is not a completed result. Do not submit again.",
        }[status]
        value["hint"] = get
    elif status == "cancelling":
        value["message"] = "Cancellation requested; execution has not stopped yet. Do not repeat cancel or resume while cancelling."
        value["hint"] = follow
    elif status in TERMINAL | {"succeeded", "skipped"}:
        value["message"] = f"Execution reached terminal status: {status}." + (
            " Execution succeeded." if status in {"finished", "succeeded"}
            else " This does not indicate successful completion."
        )
        value["hint"] = logs
        if status in {"finished", "succeeded"}:
            suffix = "json" if execution else "jsonl"
            filename = f"task-{task_id}" + (f"-{execution}" if execution else "")
            value["hint"] = f"flowork-cli task download {target} --file /data/{filename}.{suffix}"
        if result.get("can_resume") is True:
            value["message"] += " This batch can resume unfinished/failed rows, only if the user wants to continue."
        elif result.get("can_resume") is False:
            value["message"] += " This execution cannot be resumed."
    elif status == "deleted":
        value["message"] = "Task deleted. Its Workflow was not deleted."
        value["hint"] = f"flowork-cli task list --task-type {task_type}"
    if value.get("evaluation_status") in {"queued", "running"}:
        value["message"] = f"Inference status: {status}. Evaluation is still {value['evaluation_status']}; metrics are not ready."
        value["hint"] = follow
    elif value.get("evaluation_status") == "failed":
        value["message"] = f"Inference status: {status}. Evaluation failed; saved inference results are preserved. Read evaluation errors in logs."
        value["hint"] = logs
    if value.get("authorization_pending"):
        value["message"] += " Permissions are becoming available; do not create again."
        value["hint"] = f"flowork-cli task list --task-type {task_type}"
    return value


def _schedule(value):
    # The internal schedule row ID is not a CLI addressable resource.
    return {key: item for key, item in value.items() if key != "id"}


def _execution(value, task_id):
    execution_id = value.get("execution_id") or value["id"]
    result = {key: item for key, item in value.items() if key not in {"id", "schedule_id", "run_key", "results_uri", "error"}}
    result["execution_error"] = value.get("execution_error", value.get("error"))
    return _feedback(result, task_id, execution=execution_id)


def _task(value):
    value = dict(value)
    value["task_id"] = value.pop("id")
    # A failed execution is still a successful observation, not a CLI error.
    value["task_error"] = value.pop("error", None)
    value["task_type"] = "schedule_run" if value.get("task_type") == "scheduled_run" else value.get("task_type")
    payload = dict(value.pop("payload", None) or {})
    payload.pop("schedule_id", None)
    snapshot = payload.pop("workflow_snapshot", None) or {}
    if snapshot:
        value["version"] = snapshot.get("version")
    # Do not flood context with the input dataset, internal queue IDs or object keys.
    data = payload.pop("data_source", None) or {}
    if data:
        value["rows"] = len(data.get("rows", []))
    value["config"] = payload
    value.pop("background_job_id", None)
    value.pop("results_uri", None)
    return value


def _info(task):
    value = _task(task)
    return {key: value[key] for key in ("task_id", "task_type", "workflow_id", "version", "config", "rows", "submitted_at") if key in value}


def _batch_status(task):
    value = _task(task)
    for key in ("config", "version", "rows", "allowed_actions", "provenance"):
        value.pop(key, None)
    return _feedback(value, task["id"])


def _batch_history(task, events):
    """Project durable start/terminal events into attempts, including pre-start failures."""
    attempts = []
    current = None
    task_id = task["id"]
    for event in events:
        payload = event.get("payload") or {}
        if payload.get("action") in {"batch.started", "batch.resume_started"}:
            if current and current["finished_at"] is None:
                current["status"] = "unknown"
                current["before"] = event["id"]
            current = {"attempt": len(attempts) + 1, "status": "running", "started_at": event["ts"],
                       "finished_at": None, "after": event["id"] - 1, "before": None,
                       "trigger": "resume" if payload["action"] == "batch.resume_started" else "submit"}
            attempts.append(current)
        elif event["event_type"] == "terminal":
            if current is None or current["finished_at"] is not None:
                current = {"attempt": len(attempts) + 1, "started_at": None,
                           "after": event["id"] - 1, "trigger": "submit"}
                attempts.append(current)
            current.update(status=payload.get("task_status") or "unknown", finished_at=event["ts"],
                           before=event["id"] + 1, message=payload.get("message"), error=payload.get("error"))
    if current and current["finished_at"] is None:
        current["status"] = task["status"]
    for attempt in attempts:
        command = f"flowork-cli task logs --task-id {task_id} --task-type batch_exec --after {attempt['after']}"
        if attempt["before"] is not None:
            command += f" --before {attempt['before']}"
        attempt["hint"] = command
    return list(reversed(attempts))


async def _history(task, common, *, limit=20, offset=0):
    task_id = uuid.UUID(task["id"])
    if task["task_type"] == "scheduled_run":
        result = await routes.list_scheduled_run_executions(task_id, **common, limit=limit, offset=offset)
        items = [_execution(item, task_id) for item in result["items"]]
        total = result["total"]
    else:
        events = []
        cursor = 0
        while True:
            page = await routes.list_task_events(task_id, **common, after_seq=cursor, before_seq=None,
                event_type=["state", "terminal"], limit=200, from_=None, to=None, order="asc")
            events.extend(page["items"])
            if not page["next_cursor"] or not page["items"]:
                break
            cursor = page["items"][-1]["id"]
        attempts = _batch_history(task, events)
        total = len(attempts)
        items = attempts[offset:offset + limit]
    result = {"task_id": str(task_id), "task_type": "schedule_run" if task["task_type"] == "scheduled_run" else "batch_exec",
              "history": items, "next_offset": offset + len(items) if offset + len(items) < total else None}
    return result


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _schedule_body(arguments, *, create):
    result = {key: arguments[key] for key in ("name", "workflow_id", "major", "version", "timezone") if key in arguments}
    for key in ("start_at", "end_at", "run_at"):
        if key in arguments:
            result[key] = _parse_time(arguments[key])
    if "run_at" in arguments:
        result.update(schedule_type="once", cron_expr=None, interval_seconds=None, start_at=None, end_at=None)
    if "interval" in arguments:
        result.update(schedule_type="interval", interval_seconds=arguments["interval"], cron_expr=None)
    if "cron" in arguments:
        result.update(schedule_type="cron", cron_expr=arguments["cron"], interval_seconds=None)
    if "inputs" in arguments:
        result["input_preset"] = arguments["inputs"]
    if "mount" in arguments:
        result["mount_enabled"] = arguments["mount"]
    if "notify" in arguments:
        events = [] if arguments["notify"] == "none" else arguments["notify"].split(",")
        result["notification_policy"] = {"enabled": bool(events), "on": events, "email": arguments.get("notify_email", "")}
    if create:
        result.setdefault("name", arguments["workflow_id"] + " schedule")
        result["enabled"] = not arguments.get("paused", False)
        return routes.ScheduledRunCreateBody(**result)
    return routes.ScheduledRunPatchBody(**result)


from vibecanvas_api.services.agent_resources.table_io import _text_to_rows, _xlsx_to_rows

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


def prepare_batch(arguments, snapshot):
    """Match input names; turn repeated flat output mappings into the UI schema."""
    data = base64.b64decode(arguments["data"], validate=True)
    sheet = arguments.get("input_sheet", "")
    if arguments["format"] in {"xlsx", "xlsm"} and not sheet:
        import openpyxl
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            sheet = book.sheetnames[0]
        finally:
            book.close()
    rows = _batch_rows({**arguments, "name": "task input", "sheet": sheet})
    if not rows:
        raise ToolError("empty_input", "The input table has no data rows.")
    graph = snapshot["workflow"]
    starts = [node for node in graph.values() if isinstance(node, dict) and node.get("node_type") == "StartNode"]
    if len(starts) != 1:
        raise ToolError("invalid_workflow", "Workflow must have exactly one StartNode.")
    fields = starts[0].get("input_fields") or {}
    normalized = []
    for index, row in enumerate(rows):
        missing = [key for key, spec in fields.items() if key not in row and "value" not in spec]
        if missing:
            raise ToolError("missing_input", f"Input row {index}: missing fields {', '.join(missing)}. Available columns: {', '.join(row)}.")
        normalized.append({key: row[key] if key in row else deepcopy(spec["value"]) for key, spec in fields.items()})
    columns = [{"kind": key, "name": key} for key in FIXED_COLUMNS]
    for index, mapping in enumerate(arguments.get("mapping", []), 1):
        node_id, field = mapping["source"].split(".", 1)
        node = graph.get(node_id)
        if not isinstance(node, dict) or field not in (node.get("output_fields") or {}):
            raise ToolError("invalid_mapping", f"Mapping {index}: output {mapping['source']!r} is not declared in the selected workflow version.")
        # The public CLI selects stable node IDs; engine result dictionaries
        # use node_name keys. Freeze that translation with the graph snapshot.
        column = {"kind": "field", "name": mapping["field"], "node": node.get("node_name") or node_id, "field": field}
        if "default" in mapping:
            column["default"] = mapping["default"]
        columns.append(column)
    output = None
    if arguments.get("output_path"):
        output = {"type": "vfs_data", "path": arguments["output_path"]}
        if arguments.get("output_sheet"):
            output["sheet_name"] = arguments["output_sheet"]
    body = workflows.BatchSubmitBody(notification_policy={"on": [] if arguments.get("notify", "none") == "none" else arguments["notify"].split(","), "email": arguments.get("notify_email", "")}, evaluation={"enabled": True, "script": arguments["evaluation_script"]} if arguments.get("evaluation_script") else {}, data_source={"rows": normalized},
        column_mapping={key: key for key in fields}, output=output, output_columns=columns,
        concurrency=arguments.get("concurrency", 1), version=snapshot["version"], mount_enabled=arguments.get("mount", False))
    return body, sheet


async def _read(ctx, operation, arguments, emit):
    if operation == "task.list":
        async def page(params, offset):
            return await routes.list_tasks(**params, status=[], task_type=[],
                workflow_id=None, q=None, limit=100, offset=offset)
        rows = await visible_resource_rows(ctx, "task", list_page=page, get_resource=routes.get_task)
        values = [_task(row) for row in rows]
        statuses = arguments.get("status", "").split(",") if arguments.get("status") else []
        values = [row for row in values
                  if (not statuses or row.get("status") in statuses)
                  and (not arguments.get("task_type") or row.get("task_type") == arguments["task_type"])
                  and (not arguments.get("workflow_id") or row.get("workflow_id") == arguments["workflow_id"])]
        values.sort(key=lambda row: (str(row.get("submitted_at") or ""), str(row["task_id"])), reverse=True)
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 20)
        return {"tasks": [{"name": (row.get("config") or {}).get("name"),
                **{key: row.get(key) for key in ("task_id", "task_type", "workflow_id", "status", "progress", "submitted_at")}}
                for row in values[offset:offset + limit]],
                "next_offset": offset + limit if offset + limit < len(values) else None}
    task_id = uuid.UUID(arguments["task_id"]) if "task_id" in arguments else None
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
        common = (await admitted_resource_route_params(ctx, session, "task", task_id)
                if task_id else resource_route_params(ctx, session))
        task = await routes.get_task(task_id, **common)
        expected = "scheduled_run" if arguments["task_type"] == "schedule_run" else "batch_exec"
        if task["task_type"] != expected:
            raise ToolError("wrong_task_type", "--task-type does not match the stored Task type. No operation was performed.")
        if operation == "task.info":
            value = _info(task)
            if expected == "scheduled_run":
                plan = await routes.get_scheduled_run(task_id, **common)
                value.pop("config", None)
                value["version"] = (plan["schedule"].get("workflow_selector") or {}).get("version")
                value["schedule"] = {key: item for key, item in _schedule(plan["schedule"]).items() if key not in {"last_status", "last_run_at"}}
                return _feedback(value, task_id, plan=True)
            return value
        if operation in {"task.status", "task.logs", "task.download"}:
            if (expected == "scheduled_run") != bool(arguments.get("execution_id")):
                raise ToolError("invalid_arguments", "schedule_run requires --execution-id; batch_exec rejects it. Use task history to discover executions.")
        if operation == "task.status":
            if arguments.get("execution_id"):
                execution = await routes.get_scheduled_run_execution(task_id, uuid.UUID(arguments["execution_id"]), **common)
                return _execution(execution, task_id)
            return _batch_status(task)
        if operation == "task.history":
            if expected != "scheduled_run":
                raise ToolError("invalid_arguments", "Batch tasks have no execution history; use task logs.")
            return await _history(task, common, limit=arguments.get("limit", 20), offset=arguments.get("offset", 0))
        if operation == "task.download":
            if expected == "scheduled_run":
                response = await routes.download_scheduled_execution_results(task_id, uuid.UUID(arguments["execution_id"]), **common)
            else:
                response = await routes.download_results(task_id, **common, format=arguments["format"])
            size = 0
            async def chunks():
                if hasattr(response, "body_iterator"):
                    async for chunk in response.body_iterator:
                        yield chunk
                else:
                    # On-demand XLSX is a normal Response, not StreamingResponse.
                    for offset in range(0, len(response.body), 64 * 1024):
                        yield response.body[offset:offset + 64 * 1024]
            try:
                async for chunk in chunks():
                    if isinstance(chunk, str):
                        chunk = chunk.encode()
                    size += len(chunk)
                    await emit({"progress": {"chunk": base64.b64encode(chunk).decode("ascii")}})
            finally:
                if hasattr(getattr(response, "body_iterator", None), "aclose"):
                    await response.body_iterator.aclose()
            return {"task_id": str(task_id), "task_type": arguments["task_type"],
                    **({"execution_id": arguments["execution_id"]} if arguments.get("execution_id") else {}),
                    "format": arguments["format"], "bytes": size, "message": "Results downloaded."}
        if operation == "task.logs":
            exporting = arguments.get("export", False)
            end = _parse_time(arguments.get("to_time"))
            start = _parse_time(arguments.get("from_time"))
            if start and end and start > end:
                raise ToolError("invalid_arguments", "from must be before to.")
            execution = None
            if arguments.get("execution_id"):
                execution = await routes.get_scheduled_run_execution(task_id, uuid.UUID(arguments["execution_id"]), **common)
            page = await routes.list_task_events(task_id, **common, after_seq=arguments.get("after", 0),
                before_seq=arguments.get("before"), event_type=[], limit=arguments.get("limit", 100),
                from_=start, to=end, order="asc",
                execution_id=uuid.UUID(arguments["execution_id"]) if execution else None)
            items = page["items"]
            cursor = items[-1]["id"] if items else arguments.get("after", 0)
            terminal = execution["status"] in {"succeeded", "failed", "cancelled", "skipped"} if execution else task["status"] in TERMINAL
            evaluation_status = None
            if not execution:
                payload = task.get("payload") or {}
                records = payload.get("evaluations") or []
                evaluation_status = ("failed" if payload.get("evaluation_queue_error") else
                    records[0]["status"] if records else "not_run" if
                    (payload.get("evaluation") or {}).get("enabled") else "not_configured")
                if evaluation_status in {"queued", "running"}:
                    terminal = False
            observed = execution if execution else task
            result = {"task_id": str(task_id), "logs": items, "cursor": cursor, "has_more": page["next_cursor"] is not None, "terminal": terminal,
                        **({"execution_id": execution["id"]} if execution else {}),
                        "status": observed["status"],
                        **({"evaluation_status": evaluation_status} if evaluation_status else {}),
                        "execution_error" if execution else "task_error": observed.get("error"),
                        "result": observed.get("result")}
            result = _feedback(result, task_id, execution=execution["id"] if execution else None)
            if exporting:
                history = await _history(task, common) if execution else {"history": [], "next_offset": None}
                summary = {**result, "task": _task(task), "logs": None, "next_history_offset": history["next_offset"]}
                result["files"] = {"status.json": json.dumps(summary, ensure_ascii=False, indent=2),
                    "logs.jsonl": "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
                    "history.jsonl": "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in history["history"])}
                if not execution:
                    result["files"].pop("history.jsonl")
                result.pop("logs", None)
            return result
    raise ToolError("unsupported_operation", "Unsupported Task query.")


async def execute(call, arguments):
    operation, cap = call.operation, call.capability
    writing = operation in WRITE_OPERATIONS
    write_started = False
    try:
        ctx = await agent_context.resolve_context(cap)
        if not writing:
            if operation == "task.logs" and arguments.get("follow"):
                query = dict(arguments)
                while True:
                    ctx = await agent_context.resolve_context(cap)
                    page = await _read(ctx, operation, query, call.emit)
                    for event in page["logs"]:
                        await call.emit({"progress": event})
                    query["after"] = page["cursor"]
                    if page["terminal"] and not page["has_more"]:
                        return _feedback({"id": arguments["task_id"], "cursor": page["cursor"], "terminal": True,
                            **{key: page[key] for key in ("status", "result", "task_error", "execution_error", "evaluation_status") if key in page}},
                            arguments["task_id"], execution=query.get("execution_id"))
                    if not page["has_more"]:
                        await asyncio.sleep(1)
            return jsonable_encoder(await _read(ctx, operation, arguments, call.emit))
        action = {"task.enable": "resume", "task.disable": "pause"}.get(operation, operation.split(".")[1])
        operation = f"task.{arguments['task_type']}.{action}"
        task_id = uuid.UUID(arguments["task_id"]) if "task_id" in arguments else None
        prepared = None
        sheet = ""
        if operation == "task.batch_exec.create":
            await require_workflow_action(ctx, arguments["workflow_id"], Action.EXECUTE)
            snapshot = await read_workflow_snapshot(ctx, workflow_id=arguments["workflow_id"], major=arguments.get("major", ""), version=arguments.get("version", ""))
            prepared, sheet = await asyncio.to_thread(prepare_batch, arguments, snapshot)
        # Preflight current type and permission before presenting any approval.
        async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
            common = (await admitted_resource_route_params(ctx, session, "task", task_id)
                if task_id else resource_route_params(ctx, session))
            current = None
            if task_id:
                action = {"cancel": Action.CANCEL, "resume": Action.RESUME, "run": Action.EXECUTE, "evaluate": Action.EXECUTE, "delete": Action.DELETE}.get(operation.rsplit(".", 1)[1], Action.UPDATE)
                await routes._authorize_task(task_id=task_id, action=action, **{key: value for key, value in common.items() if key != "session"})
                current = await TasksRepo(session).get(task_id)
                expected = "batch_exec" if arguments["task_type"] == "batch_exec" else "scheduled_run"
                if current is None or current.task_type != expected:
                    raise ToolError("wrong_task_type", "The Task does not exist or belongs to another task category.")
            else:
                await require_workflow_action(ctx, arguments["workflow_id"], Action.EXECUTE)
            needs_approval = operation not in {"task.batch_exec.cancel", "task.schedule_run.cancel", "task.schedule_run.pause"}
            if operation == "task.schedule_run.create" and arguments.get("paused"):
                needs_approval = False
            if operation == "task.schedule_run.update":
                schedule = await TasksRepo(session).get_schedule_by_task(task_id)
                needs_approval = schedule.enabled and bool(arguments.keys() - {"task_id", "task_type", "name", "notify", "notify_email"})
            if cap.approval_mode not in {"agent", "always_ask", "always_allow"}:
                raise ToolError("invalid_approval_mode", "Unknown approval mode.")
        async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
            await session.execute(text("""INSERT INTO task_cli_leases(call_id,tenant_id,run_id,operation,expires_at)
                VALUES (:id,CAST(:tenant AS uuid),:run,:operation,now()+interval '30 seconds')"""),
                {"id": call.call_id, "tenant": cap.tenant_id, "run": cap.turn_id, "operation": operation})
        call.durable_lease = True
        if cap.approval_mode == "always_ask" or (cap.approval_mode == "agent" and needs_approval):
            from .cli_delete import _approve
            summary = {key: value for key, value in arguments.items() if key not in {"data", "inputs"}}
            if prepared:
                summary.update(version=prepared.version, rows=len(prepared.data_source["rows"]))
            await _approve(call, summary, prompt="Approve " + operation.replace(".", " ") + "? " + json.dumps(summary, ensure_ascii=False) + ". Submitted Tasks continue independently of this Chat.")
            decision = "approved"
        else:
            decision = "auto_approved"
        await call.emit({"progress": {"status": decision, "message": "Operation approved. Rechecking permissions before submission."}})
        ctx = await agent_context.resolve_context(cap)
        async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
            await _require_active_chat_write(session, ctx)
            live = (await session.execute(text("SELECT 1 FROM task_cli_leases WHERE call_id=:id AND expires_at>now()"), {"id": call.call_id})).first()
            if not live:
                raise ToolError("approval_cancelled", "The command is no longer active.")
            common = (await admitted_resource_route_params(ctx, session, "task", task_id)
                if task_id else resource_route_params(ctx, session))
            write_started = True
            if operation == "task.batch_exec.create":
                result = await workflows.submit_batch(arguments["workflow_id"], prepared, request=common["request"], ctx=common["ctx"], session=session, service=common["service"])
                if result.get("authorization_pending"):
                    return _feedback({"id": result["task_id"], "task_type": "batch_exec", "status": "queued", "version": result["version"], "authorization_pending": True}, result["task_id"])
                return _feedback({"id": result["task_id"], "task_type": "batch_exec", "status": "queued", "workflow_id": arguments["workflow_id"], "version": result["version"], "rows": len(prepared.data_source["rows"]), "input_sheet": sheet or None}, result["task_id"])
            if operation == "task.batch_exec.cancel":
                result = await routes.cancel_task(task_id, routes.CancelBody(mode="soft"), **common)
            elif operation == "task.batch_exec.resume":
                result = await routes.resume_task(task_id, **common)
            elif operation == "task.batch_exec.delete":
                result = await routes.delete_batch_task(task_id, **common)
            elif operation == "task.schedule_run.create":
                result = await routes.create_scheduled_run(_schedule_body(arguments, create=True), **common)
            elif operation == "task.schedule_run.update":
                result = await routes.update_scheduled_run(task_id, _schedule_body(arguments, create=False), **common)
            elif operation == "task.schedule_run.cancel":
                result = await routes.cancel_scheduled_run_execution(task_id, uuid.UUID(arguments["execution_id"]), **common)
            else:
                route = {"task.schedule_run.pause": routes.pause_scheduled_run,
                         "task.schedule_run.resume": routes.resume_scheduled_run,
                         "task.schedule_run.run": routes.run_scheduled_now,
                         "task.schedule_run.delete": routes.delete_scheduled_run}[operation]
                result = await route(task_id, **common)
            if "task" in result:
                return _feedback({**_task(result["task"]), "schedule": result["schedule"],
                    **({"authorization_pending": True} if result.get("authorization_pending") else {})}, result["task"]["id"], plan=True)
            execution_id = arguments.get("execution_id") or (result.get("execution") or {}).get("id")
            return jsonable_encoder(_feedback({"id": str(task_id), "task_type": arguments["task_type"], **result}, task_id,
                execution=execution_id, plan="schedule" in result))
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("task_id"):
            task_id = exc.detail["task_id"]
            return {**error(exc.detail.get("error", "task_submission_failed"),
                exc.detail.get("message", "Task submission failed."),
                f"flowork-cli task status --task-id {task_id} --task-type batch_exec"),
                "task_id": task_id, "task_type": "batch_exec", "status": exc.detail.get("status")}
        code = {403: "permission_denied", 404: "resource_unavailable", 409: "state_conflict", 422: "invalid_arguments"}.get(exc.status_code, "platform_error")
        return error(code, str(exc.detail), "Inspect task status and this command's --help before retrying.")
    except (ValueError, ValidationError) as exc:
        return error("invalid_arguments", str(exc), "Check this command's --help.")
    except ToolError as exc:
        hint = {
            "approval_denied": "Respect the decision. Do not retry or change approval mode; only a new explicit user request may start another operation.",
            "approval_cancelled": "This command's approval is no longer active. Do not reuse it or automatically resubmit the operation.",
        }.get(str(exc), "Check Task permissions, state and command arguments.")
        return error(str(exc), exc.message, hint)
    except PermissionError:
        return error("permission_denied", "The command or identity is no longer authorized.", "Use a new active Agent turn with current permissions.")
    except Exception as exc:
        logger.exception("task_cli_failed", operation=operation, error_type=type(exc).__name__)
        return uncertain_result() if write_started else error("task_operation_failed", "The platform could not complete this Task operation.", "This is not a command syntax error. Do not repeatedly retry the same command; inspect task history/logs and report the failed operation. Existing Tasks are unchanged.")
