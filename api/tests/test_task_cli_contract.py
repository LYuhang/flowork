"""Regression contract for flat, explicitly addressed Task commands."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from vibecanvas_api.flowork_cli import cli, task_cli
from vibecanvas_api.services.agent_runtime import cli_tasks


@pytest.mark.parametrize("mount", [None, "true", "false"])
@pytest.mark.parametrize("task_type", ["batch_exec", "schedule_run"])
def test_mount_is_explicit_value_and_omission_preserved(tmp_path, monkeypatch, mount, task_type):
    table = tmp_path / "rows.csv"
    table.write_text("value\n1\n")
    seen = []
    monkeypatch.setattr(cli, "request", lambda endpoint, arguments, **kw: seen.append(arguments) or {"status": "queued"})
    argv = ["task", "create", "--task-type", task_type, "--workflow-id", "wf", "--version", "v1.sv0"]
    argv += ["--input-file", str(table)] if task_type == "batch_exec" else ["--interval", "3600"]
    if mount is not None:
        argv += ["--mount", mount]
    assert cli.main(argv, socket_path="test") == 0
    if mount is None:
        assert "mount" not in seen[0]
    else:
        assert seen[0]["mount"] is (mount == "true")
    if task_type == "batch_exec":
        body, _ = cli_tasks.prepare_batch(seen[0], {"workflow": {
            "start": {"node_type": "StartNode", "input_fields": {"value": {}}}},
            "version": "v1.sv0"})
    else:
        body = cli_tasks._schedule_body(seen[0], create=True)
    assert body.mount_enabled is (mount == "true")


@pytest.mark.parametrize("mount", [None, "true", "false"])
def test_update_mount_omission_is_not_false(monkeypatch, mount):
    seen = []
    monkeypatch.setattr(cli, "request", lambda endpoint, arguments, **kw: seen.append(arguments) or {"status": "paused"})
    argv = ["task", "update", "--task-type", "schedule_run", "--task-id", str(uuid4()), "--name", "Renamed"]
    if mount is not None:
        argv += ["--mount", mount]
    assert cli.main(argv, socket_path="test") == 0
    body = cli_tasks._schedule_body(seen[0], create=False)
    assert ("mount_enabled" in body.model_fields_set) is (mount is not None)
    if mount is not None:
        assert body.mount_enabled is (mount == "true")


@pytest.mark.parametrize("invalid", [["--no_mount"], ["--no-mount"], ["--mount"], ["--mount", "yes"], ["--mount", "1"]])
def test_unsupported_mount_spellings_rejected(invalid):
    assert cli.main(["task", "create", "--task-type", "schedule_run", "--workflow-id", "wf", "--version", "v1.sv0", "--interval", "3600", *invalid]) == 2


@pytest.mark.parametrize("action", ["status", "logs", "download", "cancel"])
def test_execution_id_rules_are_symmetric(action):
    args = {"task_id": str(uuid4()), "task_type": "schedule_run"}
    with pytest.raises(ValueError, match="execution-id"):
        task_cli.validate("task." + action, args)
    args["execution_id"] = str(uuid4())
    assert task_cli.validate("task." + action, args)["execution_id"] == args["execution_id"]
    args["task_type"] = "batch_exec"
    with pytest.raises(ValueError):
        task_cli.validate("task." + action, args)
    del args["execution_id"]
    assert task_cli.validate("task." + action, args)["task_type"] == "batch_exec"


@pytest.mark.parametrize("action", ["get", "events", "diagnostics", "executions", "batch_exec", "schedule_run"])
def test_retired_commands_are_not_aliases(action):
    assert cli.main(["task", action, "--help"]) == 2


def test_public_operations_and_named_targets():
    assert task_cli.OPERATIONS == {"task." + name for name in
        ("list", "info", "status", "history", "logs", "download", "create", "update", "enable", "disable", "run", "cancel", "resume", "delete")}
    for args in (["status", str(uuid4())], ["status", "--task-id", str(uuid4())],
                 ["status", "--task-type", "batch_exec", "--task_i", str(uuid4())]):
        assert cli.main(["task", *args]) == 2


def test_batch_name_error_is_actionable_and_never_submits(tmp_path, monkeypatch, capsys):
    import json
    table = tmp_path / "rows.csv"
    table.write_text("value\n1\n")
    request = AsyncMock()
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["task", "create", "--task-type", "batch_exec", "--workflow-id", "wf",
        "--version", "v1.sv0", "--input-file", str(table), "--name", "Not supported"],
        socket_path="test") == 2
    error = json.loads(capsys.readouterr().out)
    assert "--name" in error["message"] and "batch_exec" in error["message"]
    request.assert_not_called()
    with pytest.raises(SystemExit):
        cli.main(["task", "create", "--help"])
    assert "Batch tasks do not have a custom name" in " ".join(capsys.readouterr().out.split())


def test_history_tracks_batch_resume_and_exclusive_log_boundaries():
    events = [
        {"id": 2, "event_type": "state", "ts": "t1", "payload": {"action": "batch.started"}},
        {"id": 7, "event_type": "terminal", "ts": "t2", "payload": {"task_status": "interrupted"}},
        {"id": 10, "event_type": "state", "ts": "t3", "payload": {"action": "batch.resume_started"}},
        {"id": 15, "event_type": "terminal", "ts": "t4", "payload": {"task_status": "finished"}},
    ]
    history = cli_tasks._batch_history({"id": "task", "status": "finished"}, events)
    assert [(x["attempt"], x["status"], x["after"], x["before"]) for x in history] == [
        (2, "finished", 9, 16), (1, "interrupted", 1, 8)]
    assert history[0]["trigger"] == "resume"
    assert "--task-id task --task-type batch_exec --after 9 --before 16" in history[0]["hint"]


@pytest.mark.asyncio
@pytest.mark.parametrize("saved,expected", [({}, True), ({"mount_enabled": False}, False), ({"mount_enabled": True}, True)])
@pytest.mark.parametrize("status", ["queued", "resuming"])
async def test_durable_payload_keeps_mount_on_resume_and_supports_legacy(monkeypatch, saved, expected, status):
    from vibecanvas_api import background_workflows
    task = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), user_id=uuid4(),
        workflow_id="wf", payload=saved, status=status)
    session = SimpleNamespace(get=AsyncMock(return_value=task))
    @asynccontextmanager
    async def scope():
        yield session
    monkeypatch.setattr(background_workflows, "short_admin_session", scope)
    monkeypatch.setattr(background_workflows.TasksRepo, "materialize_task", AsyncMock())
    payload = await background_workflows._load_batch_payload(str(task.id))
    assert payload["mount_enabled"] is expected
    assert payload["resume"] is (status == "resuming")


@pytest.mark.asyncio
async def test_schedule_status_selects_exact_execution_and_never_latest(monkeypatch):
    task_id, execution_id = str(uuid4()), str(uuid4())
    @asynccontextmanager
    async def scope(**kw):
        yield SimpleNamespace()
    monkeypatch.setattr(cli_tasks, "session_scope", scope)
    monkeypatch.setattr(cli_tasks, "admitted_resource_route_params", AsyncMock(return_value={
        "request": None, "ctx": None, "service": None, "session": None,
    }))
    monkeypatch.setattr(cli_tasks.routes, "get_task", AsyncMock(return_value={"task_type": "scheduled_run"}))
    get = AsyncMock(return_value={"id": execution_id, "status": "succeeded", "result": {"answer": 0}})
    listing = AsyncMock(side_effect=AssertionError("No latest-execution fallback"))
    monkeypatch.setattr(cli_tasks.routes, "get_scheduled_run_execution", get)
    monkeypatch.setattr(cli_tasks.routes, "list_scheduled_run_executions", listing)
    result = await cli_tasks._read(SimpleNamespace(tenant_id="t", username="user"), "task.status", {
        "task_id": task_id, "task_type": "schedule_run", "execution_id": execution_id}, AsyncMock())
    assert str(get.await_args.args[1]) == execution_id
    assert result["result"]["answer"] == 0 and result["status"] == "succeeded"
    listing.assert_not_called()
