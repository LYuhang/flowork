import inspect
import vibecanvas_api.routes.executions as ex
from vibecanvas_api.config import AppConfig


def test_execute_in_sandbox_kill_switch_removed():
    """The ``execute_in_sandbox`` kill switch is removed; the gVisor
    sandbox is unconditional. The config attribute no longer exists."""
    assert not hasattr(AppConfig({}), "execute_in_sandbox")


def test_workflow_producer_is_sandbox_only():
    """The workflow REST producer always uses the sandbox path.

    It delegates to ``_produce_execution_sandbox`` and the in-process ``astream``
    fallback was removed (no ``wf.astream`` / ``Workflow(...)`` loop, no flag)."""
    src = inspect.getsource(ex._produce_execution)
    assert "config.execute_in_sandbox" not in src
    assert "_produce_execution_sandbox" in src
    # The in-process fallback loop is gone.
    assert "wf.astream" not in src
    assert "Workflow(wf_dict" not in src


def test_node_producer_is_sandbox_only():
    """The node-debug producer is sandbox-only; it drives the provider's
    ``run_node`` and no longer has the in-process ``run_node_to_frames``
    fallback."""
    src = inspect.getsource(ex._produce_node_execution)
    assert "execute_history_workflow" in src and "config.execute_in_sandbox" not in src
    assert "run_node_to_frames" not in src


import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock


@pytest.fixture(autouse=True)
def history_dependencies(monkeypatch):
    """Isolate producer projection tests; real persistence is covered by canvas integration tests."""
    from contextlib import asynccontextmanager
    import vibecanvas_api.services.workflow_resources as resources

    @asynccontextmanager
    async def scope(**kwargs):
        yield None

    monkeypatch.setattr(ex, "create_execution", AsyncMock())
    monkeypatch.setattr(ex, "_fail_history", AsyncMock())
    monkeypatch.setattr(ex, "inject_into_run_context_async", AsyncMock(return_value={}))
    monkeypatch.setattr(ex, "prepare_code_pythonpath", AsyncMock(return_value=None))
    monkeypatch.setattr(resources, "prepare_execution_resources", AsyncMock(return_value={}))
    monkeypatch.setattr(ex, "session_scope", scope)
    monkeypatch.setattr(ex, "WorkflowHistoryRepo", lambda _: SimpleNamespace(get=AsyncMock(return_value={"status": "cancelled"})))


@pytest.mark.asyncio
async def test_node_producer_provider_failure_yields_error_frame(monkeypatch):
    """A provider or admission failure on the node-debug path surfaces a clear
    terminal error frame, never a silent
    in-process run. The old in-process fallback is gone."""
    from vibecanvas_api.services.sandbox.provider import SandboxUnavailable
    class _UnavailableManager:
        async def get_session(self, *args, **kwargs):
            raise SandboxUnavailable("no runsc on this box")

    monkeypatch.setattr(ex, "get_sandbox_manager", lambda: _UnavailableManager())
    async def _persist_noop(*args, **kwargs):
        return None
    monkeypatch.setattr(ex, "_with_execution_repo", _persist_noop)

    node = {"node_id": "node_1", "node_name": "n", "node_type": "CodeNode",
            "node_description": "d", "input_fields": {},
            "output_fields": {"ok": {"type": "integer", "description": "o"}},
            "node_config": {"programming_language": "python",
                            "process_fn": "def process_fn(inputs):\n    return {'ok': 1}"},
            "children": []}
    stop = asyncio.Event()
    frames = []
    async for name, payload in ex._produce_node_execution(
        stop, "n_pf", node, {}, "tenant", "wf", "user"
    ):
        frames.append((name, payload))

    statuses = [p.get("status") for _, p in frames]
    # running (pre-launch) then a terminal error — NOT a 'completed' from a
    # silent in-process run.
    assert "error" in statuses, frames
    assert "completed" not in statuses, frames
    err = next(p for _, p in frames if p.get("status") == "error")
    assert "sandbox" in err["error"].lower()


@pytest.mark.asyncio
async def test_node_producer_host_only_node_yields_error_frame(monkeypatch):
    """A node the sandbox cannot run (``classify_workflow`` raises
    ``EngineNeedsHostNode``) surfaces a CLEAR error frame — no in-process
    fallback, and the sandbox is never launched."""
    from vibecanvas_api.services.sandbox.gvisor import EngineNeedsHostNode

    def _host_only(_wf):
        raise EngineNeedsHostNode("HostOnlyNode")

    monkeypatch.setattr(ex, "classify_workflow", _host_only)
    async def _persist_noop(*args, **kwargs):
        return None
    monkeypatch.setattr(ex, "_with_execution_repo", _persist_noop)

    node = {"node_id": "node_1", "node_name": "n", "node_type": "CodeNode",
            "node_description": "d", "input_fields": {},
            "output_fields": {}, "node_config": {}, "children": []}
    stop = asyncio.Event()
    frames = []
    async for name, payload in ex._produce_node_execution(
        stop, "n_ho", node, {}, "tenant", "wf", "user"
    ):
        frames.append((name, payload))

    statuses = [p.get("status") for _, p in frames]
    assert statuses == ["error"], frames
    assert "sandbox" in frames[0][1]["error"].lower()


@pytest.mark.asyncio
async def test_node_producer_persists_complete_debug_metadata(monkeypatch):
    """A successful isolated run must not replace the prior node result with
    null inputs/type/timing metadata; the persisted file is the refresh source
    for the Run-node inspector."""
    node = {
        "node_id": "node_2",
        "node_name": "code",
        "node_type": "CodeNode",
        "node_description": "d",
        "input_fields": {},
        "output_fields": {"y": {"type": "string"}},
        "node_config": {},
        "children": [],
    }
    inputs = {"x": "world"}

    class _Manager:
        async def get_session(self, *args, **kwargs):
            return SimpleNamespace()

    async def _repo_noop(*args, **kwargs):
        return None

    persisted = AsyncMock()
    monkeypatch.setattr(ex, "get_sandbox_manager", lambda: _Manager())
    monkeypatch.setattr(ex, "classify_workflow", lambda workflow: None)
    monkeypatch.setattr(ex, "_with_execution_repo", _repo_noop)
    monkeypatch.setattr(
        ex,
        "inject_into_run_context_async",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        ex,
        "execute_history_workflow",
        AsyncMock(return_value={"result": {
            "final_outputs": {"node_2": {"y": "world!"}},
            "error_dict": {},
            "execution_time": 0.25,
        }}),
    )
    monkeypatch.setattr(ex, "write_node_result", persisted)

    frames = []
    async for name, payload in ex._produce_node_execution(
        asyncio.Event(), "n_meta", node, inputs, "tenant", "wf", "user", {}
    ):
        frames.append((name, payload))

    completed = next(p for _, p in frames if p.get("status") == "completed")
    assert completed["node_name"] == "code"
    assert completed["node_type"] == "CodeNode"
    assert completed["inputs"] == inputs
    assert completed["duration"] == 0.25
    persisted.assert_awaited_once()
    run_id, tenant_id, payload = persisted.await_args.args
    assert (run_id, tenant_id) == ("wf", "tenant")
    assert payload["node_name"] == "code"
    assert payload["node_type"] == "CodeNode"
    assert payload["inputs"] == inputs
    assert payload["execution_time"] == 0.25


@pytest.mark.asyncio
async def test_node_producer_hard_cancels_sandbox_before_cancelled_frame(monkeypatch):
    node = {
        "node_id": "node_2",
        "node_name": "slow_code",
        "node_type": "CodeNode",
        "node_description": "d",
        "input_fields": {},
        "output_fields": {},
        "node_config": {},
        "children": [],
    }
    started = asyncio.Event()
    task_cancelled = asyncio.Event()

    released = asyncio.Event()

    async def _slow_run(*args, **kwargs):
        started.set()
        try:
            await released.wait()
            return {"status": {"status": "cancelled"}}
        finally:
            task_cancelled.set()

    class _Session:
        def __init__(self):
            self.cancel_calls = []

        execute_workflow_job = staticmethod(_slow_run)

        async def close_workflow_pool(self, **kwargs):
            self.cancel_calls.append(kwargs)
            released.set()
            return {"closed": True}

    session = _Session()

    class _Manager:
        async def get_session(self, *args, **kwargs):
            return session

    async def _repo_noop(*args, **kwargs):
        return None

    persisted = AsyncMock()
    monkeypatch.setattr(ex, "get_sandbox_manager", lambda: _Manager())
    monkeypatch.setattr(ex, "classify_workflow", lambda workflow: None)
    monkeypatch.setattr(ex, "_with_execution_repo", _repo_noop)
    monkeypatch.setattr(
        ex, "inject_into_run_context_async", AsyncMock(return_value={}),
    )
    import vibecanvas_api.services.workflow_history_runner as runner

    async def observe(**kwargs):
        return await kwargs["execute"]

    monkeypatch.setattr(runner, "observe_execution", observe)
    monkeypatch.setattr(ex, "write_node_result", persisted)

    stop = asyncio.Event()
    stream = ex._produce_node_execution(
        stop, "11111111-1111-4111-8111-111111111111", node, {"x": "value"}, "tenant", "wf", "user", {},
    )
    running_name, running = await anext(stream)
    assert running_name == "EXEC_UPDATE"
    assert running["status"] == "running"

    terminal_task = asyncio.create_task(anext(stream))
    await started.wait()
    stop.set()
    terminal_name, terminal = await terminal_task

    assert terminal_name == "EXEC_UPDATE"
    assert terminal["status"] == "cancelled"
    assert session.cancel_calls == [{
        "tenant": "tenant",
        "pool_id": "11111111111141118111111111111111",
        "history": True,
    }]
    assert task_cancelled.is_set()
    persisted.assert_awaited_once()
    assert persisted.await_args.args[2]["status"] == "cancelled"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
