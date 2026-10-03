import base64
import json
from uuid import uuid4

import pytest

from vibecanvas_api.flowork_cli import cli, task_cli


def test_repeated_mapping_order_and_json_defaults(tmp_path, monkeypatch, capsys):
    source = tmp_path / "rows.csv"
    source.write_text("value\n1\n")
    seen = []

    def request(endpoint, arguments, **kwargs):
        seen.append((kwargs["operation"], arguments))
        return {"id": "task", "status": "queued"}

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["task", "create", "--task_type", "batch_exec", "--workflow_id", "wf",
        "--version", "v2.sv0", "--input_file", str(source),
        "--mapping", '{"field":"answer","source":"node_3.answer","default":false}',
        "--mapping", '{"field":"score","source":"node_3.score","default":0}'], socket_path="test") == 0
    operation, args = seen[0]
    assert operation == "task.create"
    assert args["mapping"] == [
        {"field": "answer", "source": "node_3.answer", "default": False},
        {"field": "score", "source": "node_3.score", "default": 0},
    ]
    assert base64.b64decode(args["data"]) == source.read_bytes()
    assert json.loads(capsys.readouterr().out)["status"] == "queued"


@pytest.mark.parametrize("mapping", [
    {}, {"field": "status", "source": "node.x"},
    {"field": "a", "source": "node"}, {"field": "a", "source": ".x"},
    {"field": "a", "source": "node.x", "unknown": 1},
])
def test_invalid_mapping_rejected(mapping):
    with pytest.raises(ValueError, match="Mapping 1"):
        task_cli.validate("task.create", {"task_type": "batch_exec", "workflow_id": "wf", "version": "v1.sv0", "format": "csv", "data": "", "mapping": [mapping]})


def test_mapping_names_cannot_repeat():
    with pytest.raises(ValueError, match="Mapping 2"):
        task_cli.validate("task.create", {"task_type": "batch_exec", "workflow_id": "wf", "version": "v1.sv0", "format": "csv", "data": "", "mapping": [
            {"field": "a", "source": "node.x"}, {"field": "a", "source": "node.y"},
        ]})


def test_leaf_help_explains_manual_run_and_cancel_semantics(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["task", "run", "--help"])
    assert exc.value.code == 0
    assert "does not enable the schedule" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["task", "cancel", "--help"])
    assert "schedule_run requires --execution_id" in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("args,phrase", [
    (["task", "cancel"], "interrupted"),
    (["task", "resume"], "SAME Task ID"),
    (["task", "logs"], "schedule_run requires --execution_id"),
    (["task", "create"], "ENABLED schedule by default"),
    (["task", "update"], "does not enable a paused schedule"),
    (["task", "create"], "Creation defaults to false"),
    (["task", "update"], "Frozen on submission"),
])
def test_leaf_help_explains_observed_lifecycle_edges(capsys, args, phrase):
    with pytest.raises(SystemExit) as exc:
        cli.main([*args, "--help"])
    assert exc.value.code == 0
    assert phrase in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("operation,args", [
    ("task.create", {"task_type": "batch_exec", "workflow_id": "wf", "format": "csv", "data": ""}),
    ("task.create", {"task_type": "batch_exec", "workflow_id": "wf", "version": "v1.sv0", "format": "csv", "data": "", "concurrency": 17}),
    ("task.cancel", {"task_type": "schedule_run", "task_id": str(uuid4())}),
    ("task.status", {"task_type": "batch_exec", "task_id": "not-uuid"}),
    ("task.update", {"task_type": "schedule_run", "task_id": str(uuid4())}),
    ("task.create", {"task_type": "schedule_run", "workflow_id": "wf", "version": "v1.sv0", "interval": 2, "cron": "* * * * *"}),
    ("task.create", {"task_type": "schedule_run", "workflow_id": "wf", "version": "v1.sv0", "interval": 2, "start_at": "2026-01-01T09:00:00"}),
    ("task.list", {"type": "scheduled_run"}),
    ("task.list", {"tenant_id": "arbitrary"}),
])
def test_host_rejects_invalid_contract(operation, args):
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments(operation, args)


def test_schedule_update_clear_is_explicit(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "request", lambda endpoint, arguments, **kwargs: seen.append(arguments) or {"id": arguments["task_id"]})
    task_id = str(uuid4())
    assert cli.main(["task", "update", "--task_id", task_id, "--task_type", "schedule_run", "--clear_end_at", "--mount", "false"], socket_path="test") == 0
    assert seen == [{"task_id": task_id, "task_type": "schedule_run", "end_at": None, "mount": False}]


def test_download_streams_to_atomic_file(tmp_path, monkeypatch):
    output = tmp_path / "results.jsonl"

    def request(endpoint, arguments, **kwargs):
        kwargs["on_progress"]({"chunk": base64.b64encode(b'{"ok":true}\n').decode()})
        return {"id": arguments["task_id"], "bytes": 12}

    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["task", "download", "--task_id", str(uuid4()), "--task_type", "batch_exec", "--file", str(output)], socket_path="test") == 0
    assert output.read_bytes() == b'{"ok":true}\n'
    assert not list(tmp_path.glob(".flowork-download-*"))


def test_batch_preparation_uses_declared_outputs_and_input_names():
    from vibecanvas_api.services.agent_runtime.cli_tasks import prepare_batch
    graph = {
        "start": {"node_type": "StartNode", "input_fields": {"value": {"type": "number"}, "optional": {"value": False}}},
        "code": {"node_type": "CodeNode", "output_fields": {"answer": {"type": "number"}}},
    }
    args = {"data": base64.b64encode(b'[{"value":0,"ignored":"x"}]').decode(), "format": "json", "mapping": [{"field": "a", "source": "code.answer", "default": 0}]}
    body, sheet = prepare_batch(args, {"workflow": graph, "version": "v2.sv3"})
    assert body.version == "v2.sv3"
    assert body.data_source == {"rows": [{"value": 0, "optional": False}]}
    assert body.output_columns[-1] == {"kind": "field", "name": "a", "node": "code", "field": "answer", "default": 0}
    assert sheet == ""


@pytest.mark.parametrize("task_error", [None, "A workflow node failed."])
def test_observing_a_failed_task_is_not_a_cli_failure(monkeypatch, capsys, task_error):
    from vibecanvas_api.services.agent_runtime.cli_tasks import _task
    result = _task({"id": str(uuid4()), "task_type": "batch_exec", "status": "failed", "error": task_error})
    monkeypatch.setattr(cli, "request", lambda *args, **kwargs: result)
    assert cli.main(["task", "status", "--task_id", result["task_id"], "--task_type", "batch_exec"], socket_path="test") == 0
    assert json.loads(capsys.readouterr().out)["task_error"] == task_error


@pytest.mark.asyncio
async def test_silent_execution_cancellation_does_not_wait_for_node_events(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from vibecanvas_api.background_tasks import scheduled_runs
    stop = asyncio.Event()
    monkeypatch.setattr(scheduled_runs, "_execution_cancelled", AsyncMock(side_effect=[False, True]))
    await asyncio.wait_for(scheduled_runs._watch_cancellation(uuid4(), stop), timeout=2)
    assert stop.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["approval_denied", "approval_cancelled"])
async def test_approval_errors_do_not_invite_command_retries(monkeypatch, code):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vibecanvas_api.agents.tools.decorator import ToolError
    from vibecanvas_api.services.agent_runtime import cli_tasks
    monkeypatch.setattr(cli_tasks.agent_context, "resolve_context", AsyncMock(side_effect=ToolError(code, "No changes were made.")))
    result = await cli_tasks.execute(SimpleNamespace(operation="task.run", capability=object()), {})
    assert result["error"] == code
    assert "Do not" in result["hint"]
    assert "Check Task permissions" not in result["hint"]


def test_output_mapping_resolves_node_id_to_snapshot_name_and_preserves_false_values():
    from vibecanvas_api.services.agent_runtime.cli_tasks import prepare_batch
    from vibecanvas_api.services.batch_output import serialize_results
    import csv
    import io
    graph = {
        "node_1": {"node_type": "StartNode", "input_fields": {}},
        "node_2": {"node_type": "CodeNode", "node_name": "friendly_name", "output_fields": {"zero": {}, "flag": {}, "empty": {}, "absent": {}}},
    }
    args = {"data": base64.b64encode(b'[{}]').decode(), "format": "json", "mapping": [
        {"field": field, "source": "node_2." + field, "default": "DEFAULT"}
        for field in ("zero", "flag", "empty", "absent")
    ]}
    body, _ = prepare_batch(args, {"workflow": graph, "version": "v1.sv1"})
    assert body.output_columns[-1]["node"] == "friendly_name"
    content, _ = serialize_results([{"index": 0, "status": "success", "output": {"friendly_name": {"zero": 0, "flag": False, "empty": ""}}}], path="results.csv", columns=body.output_columns)
    row = next(csv.DictReader(io.StringIO(content.decode())))
    assert (row["zero"], row["flag"], row["empty"], row["absent"]) == ("0", "False", "", "DEFAULT")


@pytest.mark.asyncio
async def test_xlsx_response_downloaded_through_cli_channel(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from starlette.responses import Response
    from vibecanvas_api.services.agent_runtime import cli_tasks
    @asynccontextmanager
    async def scope(**kwargs):
        yield SimpleNamespace()
    monkeypatch.setattr(cli_tasks, "session_scope", scope)
    monkeypatch.setattr(cli_tasks, "resource_route_params", lambda ctx, session: {
        "request": None, "ctx": None, "session": session, "service": None,
    })
    monkeypatch.setattr(cli_tasks.routes, "get_task", AsyncMock(return_value={"task_type": "batch_exec"}))
    data = b"PK" + b"x" * 150_000
    monkeypatch.setattr(cli_tasks.routes, "download_results", AsyncMock(return_value=Response(data)))
    emit = AsyncMock()
    result = await cli_tasks._read(SimpleNamespace(tenant_id="tenant"), "task.download", {"task_id": str(uuid4()), "task_type": "batch_exec", "format": "xlsx"}, emit)
    assert result["bytes"] == len(data)
    assert emit.await_count == 3
    assert b"".join(base64.b64decode(call.args[0]["progress"]["chunk"]) for call in emit.await_args_list) == data


@pytest.mark.parametrize("status,phrase", [
    ("queued", "Do not submit again"), ("running", "not a completed result"),
    ("resuming", "Do not resume again"), ("cancelling", "has not stopped yet"),
    ("failed", "does not indicate successful completion"),
    ("cancelled", "does not indicate successful completion"),
    ("finished_with_errors", "does not indicate successful completion"),
    ("interrupted", "does not indicate successful completion"),
    ("skipped", "does not indicate successful completion"),
    ("finished", "Execution succeeded"), ("succeeded", "Execution succeeded"),
])
def test_async_feedback_explains_actual_state_and_exact_next_command(status, phrase):
    from vibecanvas_api.services.agent_runtime.cli_tasks import _feedback
    task_id, execution = str(uuid4()), str(uuid4())
    result = _feedback({"status": status}, task_id, execution=execution)
    assert result["status"] == status
    assert phrase in result["message"]
    assert f"--task_id {task_id} --task_type schedule_run" in result["hint"]
    assert f"--execution_id {execution}" in result["hint"]
    assert result["execution_id"] == execution
    if status == "cancelling":
        assert "task logs" in result["hint"] and "--follow" in result["hint"]


@pytest.mark.parametrize("enabled", [True, False])
def test_schedule_feedback_is_not_an_execution_result(enabled):
    from vibecanvas_api.services.agent_runtime.cli_tasks import _feedback
    result = _feedback({"status": "failed", "schedule": {"enabled": enabled}}, "task", plan=True)
    assert ("not an execution result" if enabled else "existing executions are not cancelled") in result["message"]
    assert "history --task_id task --task_type schedule_run" in result["hint"]


def test_unknown_execution_does_not_invite_automatic_resume():
    from vibecanvas_api.services.agent_runtime.cli_tasks import _feedback
    result = _feedback({"status": "failed", "result": {"outcome_unknown": True, "can_resume": False}}, "task")
    assert "Do not automatically resume or resubmit" in result["message"]
    assert "flowork-cli task logs --task_id task --task_type batch_exec" in result["hint"]
    pending = _feedback({"status": "queued", "authorization_pending": True}, "task")
    assert "permissions are becoming available" in pending["message"].lower()
    assert "task list --task_type batch_exec" in pending["hint"]
    resumable = _feedback({"status": "interrupted", "result": {"can_resume": True}}, "task")
    assert "only if the user wants to continue" in resumable["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "finished_with_errors", "finished"])
async def test_follow_finishes_with_business_status_not_just_stream_completion(monkeypatch, capsys, status):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vibecanvas_api.services.agent_runtime import cli_tasks
    task_id = str(uuid4())
    monkeypatch.setattr(cli_tasks.agent_context, "resolve_context", AsyncMock(return_value=object()))
    read = AsyncMock(side_effect=[
        {"logs": [{"id": 1}], "cursor": 1, "terminal": False, "has_more": True, "status": "running"},
        {"logs": [{"id": 2}], "cursor": 2, "terminal": True, "has_more": False,
         "status": status, "task_error": None if status == "finished" else "Execution did not succeed.", "result": {"rows": 1}},
    ])
    monkeypatch.setattr(cli_tasks, "_read", read)
    call = SimpleNamespace(operation="task.logs", capability=object(), emit=AsyncMock())
    result = await cli_tasks.execute(call, {"task_id": task_id, "task_type": "batch_exec", "follow": True})
    assert call.emit.await_count == 2
    assert result["status"] == status and result["terminal"] is True
    assert result["cursor"] == 2
    assert "Execution event stream completed" not in result["message"]
    monkeypatch.setattr(cli, "request", lambda *args, **kwargs: result)
    assert cli.main(["task", "logs", "--task_id", task_id, "--task_type", "batch_exec", "--follow"], socket_path="test") == 0
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_status_is_the_single_query_name(monkeypatch, capsys):
    seen = []
    task_id = str(uuid4())
    monkeypatch.setattr(cli, "request", lambda endpoint, arguments, **kwargs:
        seen.append(kwargs["operation"]) or {"task_id": arguments["task_id"], "status": "queued"})
    assert cli.main(["task", "status", "--task_id", task_id, "--task_type", "batch_exec"], socket_path="test") == 0
    assert seen == ["task.status"]
    assert json.loads(capsys.readouterr().out)["task_id"] == task_id
    assert "task.get" not in task_cli.OPERATIONS
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments("task.get", {"task_id": task_id})


@pytest.mark.asyncio
async def test_schedule_history_includes_execution_commands_without_exposing_internal_ids(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vibecanvas_api.services.agent_runtime import cli_tasks
    task_id, execution_id = str(uuid4()), str(uuid4())
    @asynccontextmanager
    async def scope(**kwargs):
        yield SimpleNamespace()
    monkeypatch.setattr(cli_tasks, "session_scope", scope)
    monkeypatch.setattr(cli_tasks, "resource_route_params", lambda ctx, session: {
        "request": None, "ctx": None, "session": session, "service": None,
    })
    monkeypatch.setattr(cli_tasks.routes, "get_task", AsyncMock(return_value={
        "id": task_id, "task_type": "scheduled_run", "status": "failed",
        "payload": {"schedule_id": "private-plan"}, "error": "Last run failed."}))
    monkeypatch.setattr(cli_tasks.routes, "get_scheduled_run", AsyncMock(return_value={
        "schedule": {"id": "private-plan", "enabled": False}}))
    listing = AsyncMock(return_value={"items": [{"id": execution_id, "schedule_id": "private-plan",
        "status": "failed", "error": "Node failed.", "result": {"outcome_unknown": False}}], "total": 1})
    monkeypatch.setattr(cli_tasks.routes, "list_scheduled_run_executions", listing)
    result = await cli_tasks._read(SimpleNamespace(tenant_id="tenant"), "task.history", {"task_id": task_id, "task_type": "schedule_run"}, AsyncMock())
    assert result["task_id"] == task_id and "plan_status" not in result and "task" not in result and "schedule" not in result
    latest = result["history"][0]
    assert latest["execution_id"] == execution_id and latest["task_id"] == task_id
    assert latest["status"] == "failed" and latest["execution_error"] == "Node failed."
    assert f"task logs --task_id {task_id} --task_type schedule_run --execution_id {execution_id}" in latest["hint"]
    assert "private-plan" not in json.dumps(result)
    assert listing.await_args.kwargs["limit"] == 20


def test_task_rejects_floating_major():
    with pytest.raises(ValueError, match='fixed --version'):
        task_cli.validate('task.create', {'task_type': 'batch_exec', 'workflow_id': 'wf', 'major': 'v1', 'format': 'csv', 'data': 'x\n1\n'})
