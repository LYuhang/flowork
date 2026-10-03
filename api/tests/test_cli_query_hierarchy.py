"""Resource/execution separation and invocation log isolation."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from fastapi import HTTPException
from vibecanvas_api.flowork_cli import task_cli, deployment_cli
from vibecanvas_api.services.agent_runtime import cli_tasks, cli_deployments


def test_explicit_targets_and_batch_has_no_history():
    task = {"task_type": "batch_exec", "task_id": str(uuid4())}
    assert task_cli.validate("task.info", task) == task
    with pytest.raises(ValueError, match="history only supports"):
        task_cli.validate("task.history", task)
    dep = {"deployment_id": str(uuid4())}
    assert deployment_cli.validate("deployment.info", dep) == dep
    for action in ("status", "logs"):
        with pytest.raises(ValueError, match="execution_id"):
            deployment_cli.validate("deployment." + action, dep)
        assert deployment_cli.validate("deployment." + action, {**dep, "execution_id": str(uuid4())})
    with pytest.raises(ValueError, match="Unsupported parameters"):
        deployment_cli.validate("deployment.history", {**dep, "execution_id": str(uuid4())})
    with pytest.raises(ValueError):
        deployment_cli.validate("deployment.logs", {**dep, "execution_id": str(uuid4()), "after": -1})


def test_batch_info_and_status_separate_configuration_from_execution():
    task = {"id": str(uuid4()), "task_type": "batch_exec", "status": "failed", "progress": 0.5,
            "error": "test error", "payload": {"workflow_snapshot": {"version": "v1.sv2", "secret_internal": "hidden"},
            "data_source": {"rows": [{"value": 1}]}, "concurrency": 2}}
    info = cli_tasks._info(task)
    assert info["version"] == "v1.sv2" and info["config"] == {"concurrency": 2}
    assert not {"status", "progress", "task_error"} & info.keys()
    status = cli_tasks._batch_status(task)
    assert status["status"] == "failed" and status["task_error"] == "test error"
    assert "config" not in status and "version" not in status


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["events", "legacy", "wrong_source", "missing", "denied"])
async def test_deployment_logs_are_authorized_scoped_and_paginated(monkeypatch, scenario):
    dep_id, execution_id = str(uuid4()), str(uuid4())
    row = {"id": execution_id, "status": "succeeded", "error": None}
    executed = []
    async def execute(query, args):
        executed.append(args)
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: None if scenario == "missing" else row))
    @asynccontextmanager
    async def scope(**kwargs):
        yield SimpleNamespace(execute=execute)
    monkeypatch.setattr(cli_deployments, "session_scope", scope)
    monkeypatch.setattr(cli_deployments, "resource_route_params", lambda ctx, session: {"session": session})
    monkeypatch.setattr(cli_deployments.routes, "get_deployment", AsyncMock(return_value={}))
    authorize = AsyncMock(side_effect=HTTPException(403) if scenario == "denied" else None)
    monkeypatch.setattr(cli_deployments.routes, "_authorize_deployment", authorize)
    run = None if scenario == "legacy" else {"source_type": "deployment", "source_id": str(uuid4()) if scenario == "wrong_source" else dep_id, "last_seq": 3}
    events = AsyncMock(return_value=[{"seq": 2, "payload": {"status": "running"}}])
    monkeypatch.setattr(cli_deployments, "WorkflowHistoryRepo", lambda session: SimpleNamespace(get=AsyncMock(return_value=run), events=events))
    args = {"deployment_id": dep_id, "execution_id": execution_id, "after": 1, "limit": 1}
    if scenario in {"wrong_source", "missing", "denied"}:
        with pytest.raises(HTTPException):
            await cli_deployments.read(SimpleNamespace(tenant_id="tenant"), "deployment.logs", args)
        events.assert_not_awaited()
        if scenario == "denied":
            assert not executed
    else:
        result = await cli_deployments.read(SimpleNamespace(tenant_id="tenant"), "deployment.logs", args)
        assert result["logs_available"] == (scenario == "events")
        assert result["has_more"] == (scenario == "events")
        assert result["cursor"] == (2 if scenario == "events" else 1)
        assert result["execution_url"] == f"/workflow-executions/{execution_id}"
        assert str(executed[0]["dep"]) == dep_id and str(executed[0]["id"]) == execution_id
        authorize.assert_awaited_once()
