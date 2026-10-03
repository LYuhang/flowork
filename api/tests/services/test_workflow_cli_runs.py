"""Execution migration contracts. Auth/sandbox fixtures do not run workflows."""
import asyncio
import base64
import json
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.flowork_cli import cli
from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_runtime import cli_runs as runs


def batch_arguments(rows, concurrency=2):
    return {"workflow_id": "workflow", "major": "v1", "run_id": "a" * 32, "format": "json", "data": base64.b64encode(json.dumps(rows).encode()).decode(),
            "sheet": "", "name": "inputs.json", "concurrency": concurrency}


@pytest.fixture
def execution(monkeypatch):
    capability = SimpleNamespace(tenant_id="tenant", user_id="user", chat_id="chat", turn_id="turn", runtime_session_id="runtime")
    ctx = SimpleNamespace(tenant_id="tenant", username="user", turn_id="turn")
    session = SimpleNamespace(execute_workflow_job=AsyncMock(return_value={"result": {"final_outputs": {"__end__": {"ok": True}}, "error_dict": {}}}),
                              close_workflow_pool=AsyncMock(return_value={"closed": True}), writeback_vfs=AsyncMock())
    snapshot = AsyncMock(return_value={"id": "workflow", "version": "v1.sv8",
        "workflow": {"__meta__": {"workflow_version": 1, "workflow_subversion": 8}}})
    monkeypatch.setattr(runs.agent_context, "resolve_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(runs, "require_workflow_action", AsyncMock())
    monkeypatch.setattr(runs, "read_workflow_snapshot", snapshot)
    monkeypatch.setattr(runs, "validate_workflow_for_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(runs, "_require_session", AsyncMock(return_value=session))
    monkeypatch.setattr(runs, "prepare_code_pythonpath", AsyncMock(return_value="/overlay"))
    monkeypatch.setattr(runs, "inject_into_run_context_async", AsyncMock(return_value={"llm_credentials": {"handle": "private"}}))
    monkeypatch.setattr(runs, "create_execution", AsyncMock(side_effect=lambda **kwargs: str(uuid4())))

    async def observe(**kwargs):
        return await kwargs["execute"]

    monkeypatch.setattr(runs, "observe_execution", observe)
    for name in ("reserve", "renew", "release"):
        monkeypatch.setattr(runs.cli_run_lease, name, AsyncMock(return_value=True))
    return capability, session, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,local_override", [
    ("workflow.run", False), ("workflow.run", True), ("workflow.run-batch", False),
])
async def test_unbound_run_fails_without_starting_a_sandbox_job(execution, monkeypatch, operation, local_override):
    capability, session, snapshot = execution
    snapshot.side_effect = ToolError("workflow_unavailable", "The explicit workflow is unavailable.")
    arguments = batch_arguments([{}]) if operation.endswith("run-batch") else {"workflow_id": "workflow", "major": "v1", "inputs": {}}
    if local_override:
        arguments["workflow"] = {}
    run = runs.Run(capability, "a" * 32, operation, asyncio.Queue(8))
    await runs._work(run, arguments)
    result = run.queue.get_nowait()
    assert result["error"] == "workflow_unavailable" and result["terminal"] is True
    assert result["exit_code"] == 1
    session.execute_workflow_job.assert_not_awaited()
    session.close_workflow_pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_snapshot_authorization_ignores_disconnected_chat_pointer(execution):
    capability, session, snapshot = execution
    ctx = runs.agent_context.resolve_context.return_value
    ctx.current_workflow_id = None
    ctx.workflow = {}
    run = runs.Run(capability, "a" * 32, "workflow.run", asyncio.Queue(8))
    run.workflow_id = "original-snapshot-resource"
    assert await runs._authorize(run) is ctx
    assert runs.require_workflow_action.await_args.args[1] == "original-snapshot-resource"
    snapshot.assert_not_awaited()
    session.close_workflow_pool.assert_not_awaited()
    assert not run.stopping


@pytest.mark.asyncio
@pytest.mark.parametrize("node_error", [False, True])
async def test_single_node_uses_frozen_snapshot_isolated_pool_and_node_output(execution, monkeypatch, node_error):
    capability, session, snapshot = execution
    selected = {"node_2": {"node_id": "node_2", "node_type": "CodeNode", "node_config": {}}}
    selector = AsyncMock(return_value=selected)
    monkeypatch.setattr(runs, "select_execution_node", selector)
    session.execute_workflow_job.return_value = {"result": {
        "final_outputs": {} if node_error else {"node_2": {"answer": 42}},
        "error_dict": {"node_2": "Node failed."} if node_error else {},
    }}
    run = runs.Run(capability, "a" * 32, "workflow.run", asyncio.Queue(8))
    await runs._work(run, {"workflow_id": "workflow", "major": "v1", "inputs": {"value": 1}, "node": "node_2"})
    events = [run.queue.get_nowait() for _ in range(run.queue.qsize())]
    result = next(event["result"] for event in events if "result" in event)
    assert result["node_id"] == "node_2" and result["node_type"] == "CodeNode"
    assert result["version"] == "v1.sv8" and result["source"] == "saved"
    assert runs.create_execution.await_args.kwargs["workflow_version"] is None
    assert result["output"] == (None if node_error else {"answer": 42})
    assert events[-1]["exit_code"] == int(node_error)
    if node_error:
        assert any(event.get("errors") == {"node_2": "Node failed."} for event in events)
        assert "flowork-cli workflow get-spec --type CodeNode" in events[-1]["hint"]
    else:
        assert "hint" not in events[-1]
    assert selector.await_args.args[0] == snapshot.return_value["workflow"]
    runs.validate_workflow_for_context.assert_not_awaited()
    assert session.execute_workflow_job.await_args.kwargs["node_id"] == "node_2"
    assert session.execute_workflow_job.await_args.kwargs["execution_pool_id"] == run.run_id
    assert session.execute_workflow_job.await_args.kwargs["workflow"] is selected
    assert runs.prepare_code_pythonpath.await_args.args[0] is selected
    assert runs.inject_into_run_context_async.await_args.args[1] is selected
    session.close_workflow_pool.assert_awaited_once_with(tenant="tenant", pool_id=run.run_id, history=True)


@pytest.mark.asyncio
async def test_single_node_local_file_does_not_read_saved_graph(execution, monkeypatch):
    capability, session, snapshot = execution
    graph = {"node_2": {"node_id": "node_2", "node_type": "CodeNode"}}
    monkeypatch.setattr(runs, "select_execution_node", AsyncMock(return_value=graph))
    session.execute_workflow_job.return_value = {"result": {"final_outputs": {"node_2": None}, "error_dict": {}}}
    run = runs.Run(capability, "a" * 32, "workflow.run", asyncio.Queue(8))
    await runs._work(run, {"workflow_id": "workflow", "major": "v1", "inputs": {}, "node": "node_2", "workflow": graph})
    assert run.reference == {"id": "workflow", "source": "file", "version": "v1.sv8", "node_id": "node_2", "node_type": "CodeNode"}
    snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_node_interrupt_closes_only_its_owned_pool(execution, monkeypatch):
    capability, session, _ = execution
    monkeypatch.setattr(runs, "select_execution_node", AsyncMock(return_value={"node_2": {"node_type": "CodeNode"}}))
    entered, killed = asyncio.Event(), asyncio.Event()
    async def execute(**kwargs):
        assert kwargs["node_id"] == "node_2"
        entered.set()
        await killed.wait()
        return {"status": {"status": "cancelled"}, "result": None}
    async def close(**kwargs):
        killed.set()
        return {"closed": True}
    session.execute_workflow_job.side_effect = execute
    session.close_workflow_pool.side_effect = close
    run = runs.Run(capability, "a" * 32, "workflow.run", asyncio.Queue(8))
    task = asyncio.create_task(runs._work(run, {"workflow_id": "workflow", "major": "v1", "inputs": {}, "node": "node_2"}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    session.close_workflow_pool.assert_awaited_once_with(tenant="tenant", pool_id=run.run_id, history=True)


@pytest.mark.asyncio
async def test_batch_freezes_selected_snapshot_and_emits_before_slowest_row(execution):
    capability, session, snapshot = execution
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def execute(**kwargs):
        if kwargs["inputs"]["value"] == 0:
            blocked.set()
            await release.wait()
        assert kwargs["workflow"]["__meta__"]["workflow_version"] == 1
        assert kwargs["extra"] == {"llm_credentials": {"handle": "private"}, "code_pythonpath": "/overlay"}
        return {"result": {"final_outputs": {"__end__": kwargs["inputs"]}, "error_dict": {}}}

    session.execute_workflow_job.side_effect = execute
    run = runs.Run(capability, "a" * 32, "workflow.run-batch", asyncio.Queue(8))
    task = asyncio.create_task(runs._work(run, batch_arguments([{"value": 0}, {"value": 1}])))
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        assert (await run.queue.get())["version"] == "v1.sv8"
        early = await asyncio.wait_for(run.queue.get(), 5)
        assert early["record"]["index"] == 1 and early["completed"] == 1
        assert not task.done()
        # A later Chat switch cannot trigger another resolution at completion.
        snapshot.side_effect = AssertionError("must not resolve the pointer again")
        release.set()
        await asyncio.wait_for(task, 5)
        assert (await run.queue.get())["record"]["index"] == 0
        assert (await run.queue.get())["status"] == "completed"
        assert snapshot.await_count == 1
        assert len({call.kwargs["run_subpath"] for call in session.execute_workflow_job.await_args_list}) == 2
        assert {call.kwargs["execution_pool_id"] for call in session.execute_workflow_job.await_args_list} == {run.run_id}
        session.close_workflow_pool.assert_awaited_once_with(tenant="tenant", pool_id=run.run_id, history=True)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_row_errors_continue_and_exit_nonzero(execution):
    capability, session, snapshot = execution
    snapshot.return_value["workflow"]["node"] = {"node_type": "LoopBeginNode"}
    session.execute_workflow_job.side_effect = [
        {"result": {"final_outputs": {}, "error_dict": {"node": "Invalid input."}}},
        {"result": {"final_outputs": {"__end__": "ok"}, "error_dict": {}}},
    ]
    run = runs.Run(capability, "a" * 32, "workflow.run-batch", asyncio.Queue(8))
    await runs._work(run, batch_arguments([{}, {}], concurrency=1))
    events = [run.queue.get_nowait() for _ in range(run.queue.qsize())]
    assert [event["record"]["status"] for event in events if "record" in event] == ["error", "success"]
    assert events[-1]["completed"] == 2 and events[-1]["failed"] == 1 and events[-1]["exit_code"] == 1
    assert "flowork-cli workflow get-spec --type LoopBeginNode" in events[-1]["hint"]


@pytest.mark.asyncio
async def test_cancel_explicitly_stops_sandbox_job(execution):
    capability, session, _ = execution
    entered = asyncio.Event()
    killed = asyncio.Event()

    async def execute(**kwargs):
        entered.set()
        await killed.wait()
        return {"status": {"status": "cancelled"}, "result": None}

    session.execute_workflow_job.side_effect = execute
    def close_pool(**kwargs):
        killed.set()
        return {"closed": True}
    session.close_workflow_pool.side_effect = close_pool
    run = runs.Run(capability, "a" * 32, "workflow.run", asyncio.Queue(8))
    task = asyncio.create_task(runs._work(run, {"workflow_id": "workflow", "major": "v1", "inputs": {}}))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    session.close_workflow_pool.assert_awaited_once_with(tenant="tenant", pool_id="a" * 32, history=True)
    assert not run.active
    assert run.cancellation_confirmed


@pytest.mark.asyncio
async def test_file_override_keeps_explicit_resource_version_but_runs_local_graph(execution):
    capability, session, snapshot = execution
    run = runs.Run(capability, "a" * 32, "workflow.run", asyncio.Queue(8))
    await runs._work(run, {"workflow_id": "workflow", "major": "v1", "inputs": {}, "workflow": {}})
    snapshot.assert_awaited_once()
    assert session.execute_workflow_job.await_args.kwargs["workflow"] == {}
    events = [run.queue.get_nowait() for _ in range(run.queue.qsize())]
    assert all(event["source"] == "file" and event["version"] == "v1.sv8" for event in events)
    assert events[-1]["exit_code"] == 0


@pytest.mark.asyncio
async def test_kill_request_without_exit_confirmation_is_not_reported_stopped(execution):
    capability, session, _ = execution
    session.close_workflow_pool.return_value = {"closed": False}
    entered = asyncio.Event()

    async def execute(**kwargs):
        entered.set()
        await asyncio.Future()

    session.execute_workflow_job.side_effect = execute
    run = runs.Run(capability, "a" * 32, "workflow.run", asyncio.Queue(8))
    task = asyncio.create_task(runs._work(run, {"workflow_id": "workflow", "major": "v1", "inputs": {}}))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    session.close_workflow_pool.assert_awaited_once()
    assert run.cancellation_confirmed is False


@pytest.mark.asyncio
async def test_batch_stop_does_not_feed_next_row(execution):
    capability, session, _ = execution
    entered, killed = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        entered.set()
        await killed.wait()
        return {"status": {"status": "cancelled"}, "result": None}

    session.execute_workflow_job.side_effect = execute
    def close_pool(**kwargs):
        killed.set()
        return {"closed": True}
    session.close_workflow_pool.side_effect = close_pool
    run = runs.Run(capability, "a" * 32, "workflow.run-batch", asyncio.Queue(8))
    task = asyncio.create_task(runs._work(run, batch_arguments([{"row": 1}, {"row": 2}], concurrency=1)))
    await asyncio.wait_for(entered.wait(), 5)
    run.stopping = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert session.execute_workflow_job.await_count == 1
    assert session.execute_workflow_job.await_args.kwargs["execution_pool_id"] == run.run_id
    session.close_workflow_pool.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_replays_until_ack_and_is_scoped_to_turn(execution, monkeypatch):
    capability, _, _ = execution
    started = asyncio.Event()

    async def work(run, args):
        await run.emit(status="running", record={"index": 0})
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(runs, "_work", work)
    args = {"run_id": "b" * 32, "inputs": {}}
    try:
        await runs.command(capability, "workflow.run", args)
        await asyncio.wait_for(started.wait(), 5)
        poll = {"run_id": args["run_id"], "ack": 0}
        first = await runs.command(capability, "workflow.run.poll", poll)
        assert first == await runs.command(capability, "workflow.run.poll", poll)
        other = SimpleNamespace(**{**vars(capability), "turn_id": "other"})
        assert (await runs.command(other, "workflow.run.poll", poll))["error"] == "result_unknown"
        assert (await runs.command(capability, "workflow.run.poll", {**poll, "ack": 1}))["event"] is None
    finally:
        await runs.cancel_turn_runs("tenant", "chat", "turn")
    assert not any(key[:4] == ("tenant", "user", "chat", "turn") for key in runs._runs)


@pytest.mark.asyncio
async def test_lease_loss_cancels_and_forgets_original_job(execution, monkeypatch):
    capability, _, _ = execution
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def work(run, args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(runs, "_work", work)
    monkeypatch.setattr(runs, "LEASE_SECONDS", -1)
    try:
        await runs.command(capability, "workflow.run", {"run_id": "c" * 32, "inputs": {}})
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(cancelled.wait(), 5)
        assert (*runs._scope(capability), "c" * 32) not in runs._runs
    finally:
        await runs.cancel_turn_runs("tenant", "chat", "turn")


@pytest.mark.parametrize("rows", [[1], {"rows": "not a list"}, [{"value": float("nan")} ]])
def test_invalid_batch_tables_rejected_before_execution(rows):
    with pytest.raises(runs.ToolError, match="invalid_input"):
        runs._batch_rows(batch_arguments(rows))


def test_large_concurrency_is_not_arbitrarily_capped():
    arguments = batch_arguments([], concurrency=10000)
    assert cli.validate_arguments("workflow.run-batch", arguments)["concurrency"] == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize('node_error', [False, True])
async def test_result_poll_during_lease_release_keeps_real_result(execution, monkeypatch, node_error):
    capability, session, _ = execution
    if node_error:
        session.execute_workflow_job.return_value = {'result': {
            'final_outputs': {}, 'error_dict': {'node_7': "Missing output key 'doubled'"},
        }}
    releasing, finish_release = asyncio.Event(), asyncio.Event()

    async def release(*args):
        releasing.set()
        await finish_release.wait()

    monkeypatch.setattr(runs.cli_run_lease, 'release', release)
    # Reproduce a poll arriving after DELETE but before _work clears the flag.
    renew = AsyncMock(return_value=False)
    monkeypatch.setattr(runs.cli_run_lease, 'renew', renew)
    stop = AsyncMock()
    monkeypatch.setattr(runs, '_stop', stop)
    run = runs.Run(capability, 'release-race', 'workflow.run', asyncio.Queue(8))
    key = (*runs._scope(capability), run.run_id)
    runs._runs[key] = run
    work = asyncio.create_task(runs._work(run, {'workflow_id': 'workflow', 'major': 'v1', 'inputs': {}}))
    poll = None
    try:
        await asyncio.wait_for(releasing.wait(), 5)
        poll = asyncio.create_task(runs.command(capability, 'workflow.run.poll', {'run_id': run.run_id, 'ack': 0}))
        await asyncio.sleep(0)
        renew.assert_not_awaited()
        assert not poll.done()
        finish_release.set()
        await asyncio.wait_for(work, 5)
        first = await asyncio.wait_for(poll, 5)
        assert 'error' not in first
        events = [first['event']]
        ack = first['sequence']
        while not events[-1].get('terminal'):
            response = await runs.command(capability, 'workflow.run.poll', {'run_id': run.run_id, 'ack': ack})
            ack = response['sequence']
            events.append(response['event'])
        result = next(event['result'] for event in events if 'result' in event)
        assert bool(result['errors']) is node_error
        assert events[-1]['exit_code'] == int(node_error)
        assert runs.create_execution.await_args.kwargs['workflow_version'] == 'v1.sv8'
        stop.assert_not_awaited()
        renew.assert_not_awaited()
    finally:
        finish_release.set()
        await asyncio.gather(work, *([poll] if poll else []), return_exceptions=True)
        runs._runs.pop(key, None)


@pytest.mark.asyncio
async def test_real_durable_lease_loss_still_stops_execution(execution, monkeypatch):
    capability, _, _ = execution
    run = runs.Run(capability, 'expired-lease', 'workflow.run', asyncio.Queue(8))
    run.durable_lease = True
    key = (*runs._scope(capability), run.run_id)
    runs._runs[key] = run
    monkeypatch.setattr(runs.cli_run_lease, 'renew', AsyncMock(return_value=False))
    stop = AsyncMock()
    monkeypatch.setattr(runs, '_stop', stop)
    try:
        result = await runs.command(capability, 'workflow.run.poll', {'run_id': run.run_id, 'ack': 0})
        assert result['error'] == 'result_unknown'
        stop.assert_awaited_once_with(key)
    finally:
        runs._runs.pop(key, None)
