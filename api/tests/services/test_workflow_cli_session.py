"""Resident-session contracts migrated from the retired Workflow MCP wrappers."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.agent_runtime import cli_runs as runs


@pytest.fixture
def execution(monkeypatch):
    graph = {"node": {"node_id": "node", "node_type": "CodeNode",
        "input_fields": {"q": {"type": "string", "value": "configured", "reference": ""}}}}
    session = SimpleNamespace(
        execute_workflow_job=AsyncMock(return_value={"result": {
            "final_outputs": {"__end__": {"answer": 42}, "node": {"answer": 42}},
            "error_dict": {}, "execution_time": 1.25,
        }}),
        close_workflow_pool=AsyncMock(return_value={"closed": True}), writeback_vfs=AsyncMock(),
    )
    ctx = SimpleNamespace(tenant_id="tenant", username="user", turn_id="turn", _attached_session=session)
    cap = SimpleNamespace(tenant_id="tenant", user_id="user", chat_id="chat", turn_id="turn", runtime_session_id="runtime")
    monkeypatch.setattr(runs.agent_context, "resolve_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(runs, "require_workflow_action", AsyncMock())
    monkeypatch.setattr(runs, "read_workflow_snapshot", AsyncMock(return_value={"id": "workflow", "version": "v2.sv3", "workflow": graph}))
    monkeypatch.setattr(runs, "validate_workflow_for_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(runs, "select_execution_node", AsyncMock(side_effect=lambda workflow, node, ctx: workflow))
    monkeypatch.setattr(runs, "prepare_code_pythonpath", AsyncMock(return_value=None))
    monkeypatch.setattr(runs, "inject_into_run_context_async", AsyncMock(return_value={}))
    for name in ("reserve", "renew", "release"):
        monkeypatch.setattr(runs.cli_run_lease, name, AsyncMock(return_value=True))
    return SimpleNamespace(graph=graph, session=session, ctx=ctx, cap=cap)


async def execute(fixture, *, node=False, inputs=None, run_id="a" * 32):
    run = runs.Run(fixture.cap, run_id, "workflow.run", asyncio.Queue(16))
    args = {"workflow_id": "workflow", "major": "v2", "inputs": inputs or {}}
    if node:
        args["node"] = "node"
    await runs._work(run, args)
    return run, [run.queue.get_nowait() for _ in range(run.queue.qsize())]


@pytest.mark.asyncio
@pytest.mark.parametrize("node", [False, True])
@pytest.mark.parametrize("inputs", [{}, {"q": "override"}])
async def test_run_uses_attached_session_and_preserves_configured_inputs(execution, node, inputs):
    original = deepcopy(execution.graph)
    run, events = await execute(execution, node=node, inputs=inputs)
    job = execution.session.execute_workflow_job.await_args.kwargs
    assert run.session is execution.session
    assert job["inputs"] == inputs
    assert job["workflow"]["node"]["input_fields"]["q"]["value"] == "configured"
    assert execution.graph == original
    assert job["run_subpath"] == f"cli/{run.run_id}/0"
    assert job["execution_pool_id"] == run.run_id
    assert job.get("node_id") == ("node" if node else None)
    result = next(event["result"] for event in events if "result" in event)
    assert result["output"] == {"answer": 42} and result["execution_time"] == 1.25
    assert "exec_id" not in result
    assert events[-1]["status"] == "completed" and events[-1]["exit_code"] == 0
    execution.session.writeback_vfs.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("node", [False, True])
@pytest.mark.parametrize("resolver_error", [False, True])
async def test_missing_session_fails_cleanly_without_host_fallback(execution, node, resolver_error):
    del execution.ctx._attached_session
    if resolver_error:
        execution.ctx.sandbox_session = AsyncMock(side_effect=RuntimeError("private host path /secret/pool"))
    _, events = await execute(execution, node=node)
    assert events[-1]["error"] == "no_workspace"
    assert events[-1]["exit_code"] == 1
    assert "/secret" not in str(events)
    execution.session.execute_workflow_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_cli_success_waits_for_session_writeback(execution):
    entered, release = asyncio.Event(), asyncio.Event()
    async def writeback():
        entered.set()
        await release.wait()
    execution.session.writeback_vfs.side_effect = writeback
    task = asyncio.create_task(execute(execution))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not task.done()
        release.set()
        _, events = await asyncio.wait_for(task, 5)
        assert events[-1]["terminal"] and events[-1]["status"] == "completed"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_parallel_node_runs_use_unique_paths_and_pools(execution):
    completed = await asyncio.gather(*(execute(execution, node=True, run_id=f"{index:032x}") for index in range(4)))
    assert all(events[-1]["exit_code"] == 0 for _, events in completed)
    jobs = [call.kwargs for call in execution.session.execute_workflow_job.await_args_list]
    assert len({job["run_subpath"] for job in jobs}) == 4
    assert len({job["execution_pool_id"] for job in jobs}) == 4
    assert execution.session.close_workflow_pool.await_count == 4
