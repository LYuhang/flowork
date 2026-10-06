"""Unacknowledged events apply backpressure without dropping trace frames."""
import asyncio
from types import SimpleNamespace

import pytest

from vibecanvas_engine.runtime.executions import Execution, WorkflowRuntime


def execution(runtime):
    run = Execution('run', 'fingerprint', 'revision')
    run.approvals = SimpleNamespace(waiting=False, approvals={})
    runtime.executions[run.invocation_id] = run
    return run


@pytest.mark.asyncio
async def test_ack_releases_blocked_producer_without_losing_events():
    runtime = WorkflowRuntime(capacity=1, max_buffer_bytes=160)
    run = execution(runtime)
    event = {'type': 'diagnostic', 'value': 'x' * 80}
    await runtime._emit(run, event)
    pending = asyncio.create_task(runtime._emit(run, {**event, 'value': 'y' * 80}))
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        assert run.seq == 1
        assert run.buffered_bytes <= runtime.max_buffer_bytes
        runtime.acknowledge('run', 1)
        await asyncio.wait_for(pending, 1)
        frames = (await runtime.events('run', after=1))['events']
        assert [(f['seq'], f['value']) for f in frames] == [(2, 'y' * 80)]
        assert run.buffered_bytes <= runtime.max_buffer_bytes
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_unblocks_producer_without_ack_and_allows_terminal_event():
    runtime = WorkflowRuntime(capacity=1, max_buffer_bytes=160)
    run = execution(runtime)
    event = {'type': 'diagnostic', 'value': 'x' * 80}
    await runtime._emit(run, event)
    pending = asyncio.create_task(runtime._emit(run, event))
    await asyncio.sleep(0)
    assert not pending.done()
    run.stop.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 1)
    await runtime._emit(run, {'type': 'result', 'status': 'cancelled'})
    assert run.status == 'cancelled'
    assert [f['seq'] for f, _ in run.events] == [1, 2]


@pytest.mark.asyncio
async def test_single_oversized_event_fails_without_waiting_forever():
    runtime = WorkflowRuntime(capacity=1, max_buffer_bytes=160)
    run = execution(runtime)
    with pytest.raises(RuntimeError, match='execution_event_too_large'):
        await asyncio.wait_for(runtime._emit(run, {'type': 'diagnostic', 'value': 'x' * 200}), 1)
    assert run.seq == 0
    assert run.buffered_bytes == 0
