"""Runtime frames and host commands independently wake the execution driver."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.sandbox.workflow_event_wait import wait_for_execution_event


@pytest.mark.asyncio
async def test_preexisting_command_does_not_wait_for_runtime():
    changed = asyncio.Event()
    changed.set()
    client = SimpleNamespace(call=AsyncMock())
    assert await wait_for_execution_event(client, 'e', 7, changed) == ([], True)
    client.call.assert_not_awaited()
    assert not changed.is_set()


@pytest.mark.asyncio
async def test_command_interrupts_runtime_wait_and_cleans_it_up():
    started = asyncio.Event()
    closed = asyncio.Event()
    async def call(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    changed = asyncio.Event()
    task = asyncio.create_task(wait_for_execution_event(SimpleNamespace(call=call), 'e', 0, changed))
    await asyncio.wait_for(started.wait(), 1)
    changed.set()
    assert await asyncio.wait_for(task, 1) == ([], True)
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('frames', [[], [{'seq': 1, 'type': 'result'}]])
async def test_heartbeat_and_runtime_frames_are_not_host_commands(frames):
    client = SimpleNamespace(call=AsyncMock(return_value={'events': frames}))
    assert await wait_for_execution_event(client, 'e', 0, asyncio.Event()) == (frames, False)


@pytest.mark.asyncio
async def test_caller_cancellation_closes_pending_rpc():
    started = asyncio.Event()
    closed = asyncio.Event()
    async def call(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    task = asyncio.create_task(wait_for_execution_event(SimpleNamespace(call=call), 'e', 0, asyncio.Event()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_simultaneous_frame_and_command_are_both_preserved():
    changed = asyncio.Event()
    frames = [{'seq': 1, 'type': 'approval_requested'}]
    async def call(*args, **kwargs):
        changed.set()
        return {'events': frames}
    assert await wait_for_execution_event(SimpleNamespace(call=call), 'e', 0, changed) == (frames, True)
    assert not changed.is_set()


@pytest.mark.asyncio
async def test_transport_failure_reaches_driver_recovery():
    client = SimpleNamespace(call=AsyncMock(side_effect=ConnectionError('disconnected')))
    with pytest.raises(ConnectionError, match='disconnected'):
        await wait_for_execution_event(client, 'e', 0, asyncio.Event())


@pytest.mark.asyncio
async def test_driver_empty_heartbeats_do_not_query_history(monkeypatch):
    from contextlib import asynccontextmanager
    from vibecanvas_api.services.sandbox import workflow_execution_driver as module

    @asynccontextmanager
    async def scope():
        yield None
    repo = SimpleNamespace(bind_runtime=AsyncMock())
    monkeypatch.setattr(module, 'WorkflowHistoryRepo', lambda _: repo)
    waiter = AsyncMock(side_effect=[([], False), ([], False), asyncio.CancelledError()])
    monkeypatch.setattr(module, 'wait_for_execution_event', waiter)
    slot = SimpleNamespace(client=SimpleNamespace(generation='g'), alive=True,
                           process_identity=lambda: {}, invoke=AsyncMock())
    driver = module.WorkflowExecutionDriver(tenant_id='t', execution_id='e',
                                           slot=slot, persist_artifacts=AsyncMock())
    monkeypatch.setattr(driver, '_session', scope)
    monkeypatch.setattr(driver, '_persist_frames', AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await driver._run(inputs={}, context={}, changed=asyncio.Event())
    assert waiter.await_count == 3
    driver._persist_frames.assert_not_awaited()
    driver.persist_artifacts.assert_not_awaited()
