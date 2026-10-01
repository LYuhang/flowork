"""Progress observation must not retain a blocked RPC after losing its observer."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from vibecanvas_api.services import workflow_execution_history as history
from vibecanvas_api.services.deployment_completion import complete_before_cancelling


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["database", "progress"])
async def test_observer_failure_stops_owner_before_awaiting_rpc(monkeypatch, failure):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    @complete_before_cancelling
    async def execute():
        entered.set()
        await stopped.wait()
        return {"status": {"status": "cancelled"}}

    @asynccontextmanager
    async def scope(**kwargs):
        await entered.wait()
        if failure == "database":
            raise RuntimeError("database unavailable")
        yield None

    async def progress(state):
        raise RuntimeError("progress unavailable")

    close = AsyncMock(side_effect=stopped.set)
    monkeypatch.setattr(history, "short_session_scope", scope)
    monkeypatch.setattr(history.WorkflowHistoryRepo, "get", AsyncMock(return_value={"status": "waiting_approval"}))
    with pytest.raises(RuntimeError, match=f"{failure} unavailable"):
        await asyncio.wait_for(
            history.observe_execution(
                tenant_id="tenant",
                execution_id="execution",
                execute=execute(),
                on_state=progress,
                on_failure=close,
            ),
            timeout=2,
        )
    close.assert_awaited_once()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_group_close_stops_process_even_if_cancel_record_cannot_be_written(monkeypatch):
    from vibecanvas_api.services.sandbox import session_executions

    stopped = asyncio.Event()

    @asynccontextmanager
    async def unavailable(**kwargs):
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(session_executions, "short_session_scope", unavailable)
    owner = session_executions.SessionExecutions(SimpleNamespace(tenant_id="tenant"))
    pool = SimpleNamespace(close=AsyncMock(side_effect=stopped.set))
    task = asyncio.create_task(stopped.wait())
    owner.groups["group"] = session_executions.ExecutionGroup(pool, {"execution": task})
    with pytest.raises(RuntimeError, match="database unavailable"):
        await asyncio.wait_for(owner.close("group"), timeout=2)
    pool.close.assert_awaited_once()
    assert task.done()
    assert not owner.groups
    assert "group" in owner.closed


@pytest.mark.asyncio
async def test_observer_drains_committed_events_when_completion_races_a_read(monkeypatch):
    release = asyncio.Event()
    finished = asyncio.Event()
    reads = 0

    async def execute():
        await release.wait()
        finished.set()
        return {"result": "done"}

    @asynccontextmanager
    async def scope(**kwargs):
        yield None

    async def get(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            release.set()
            await finished.wait()
            return {"status": "running", "last_seq": 0}
        return {"status": "succeeded", "last_seq": 3}

    async def events(*args, after, **kwargs):
        if reads == 1:
            return []
        # Deliberately page results to verify the terminal RPC does not cause
        # remaining node events to be dropped after the first page.
        return [{"seq": after + 1}] if after < 3 else []

    monkeypatch.setattr(history, "short_session_scope", scope)
    monkeypatch.setattr(history.WorkflowHistoryRepo, "get", get)
    monkeypatch.setattr(history.WorkflowHistoryRepo, "events", events)
    on_event, on_failure = AsyncMock(), AsyncMock()
    result = await history.observe_execution(
        tenant_id="tenant",
        execution_id="execution",
        execute=execute(),
        on_state=AsyncMock(),
        on_event=on_event,
        on_failure=on_failure,
    )
    assert result == {"result": "done"}
    assert [call.args[0]["seq"] for call in on_event.await_args_list] == [1, 2, 3]
    on_failure.assert_not_awaited()
