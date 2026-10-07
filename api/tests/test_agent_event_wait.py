"""Agent streaming uses commit notifications and bounded durable replay."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services import agent_run_stream as stream
from vibecanvas_api.services import state_notifications as notifications
from vibecanvas_api.storage import agent_runs_repo, db


@pytest.mark.asyncio
async def test_idle_stream_heartbeats_without_state_queries(monkeypatch):
    changed = asyncio.Event()
    @asynccontextmanager
    async def changes(_):
        yield changed
    @asynccontextmanager
    async def session(**_):
        yield object()
    repo = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status='running')),
        list_events=AsyncMock(return_value=[]))
    monkeypatch.setattr(notifications, 'agent_changes', changes)
    monkeypatch.setattr(db, 'session_scope', session)
    monkeypatch.setattr(agent_runs_repo, 'AgentRunsRepo', lambda _: repo)
    monkeypatch.setattr(stream, 'HEARTBEAT_SECONDS', 0.02)
    events = stream.agent_run_event_stream(run_id='run', after_seq=0, tenant_id='tenant')
    try:
        for _ in range(3):
            assert 'HEARTBEAT' in str(await asyncio.wait_for(anext(events), 1))
        assert repo.get.await_count == repo.list_events.await_count == 1
        repo.get.return_value.status = 'completed'
        changed.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(events), 1)
        assert repo.get.await_count == 2
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_state_channels_do_not_cross_wake(monkeypatch):
    started = []
    async def listen(self):
        started.append(self)
        await asyncio.Event().wait()
    monkeypatch.setattr(notifications._Listener, 'listen', listen)
    async with notifications.execution_changes('same') as workflow:
        async with notifications.agent_changes('same') as agent:
            await asyncio.sleep(0)
            assert len(started) == 2
            next(item for item in started if item.channel == 'flowork_agent_state').wake('same')
            assert agent.is_set() and not workflow.is_set()
    assert not notifications._listeners


@pytest.mark.asyncio
async def test_cancel_wait_wakes_only_on_command(monkeypatch):
    from vibecanvas_api.streaming import turn_runtime
    changed, ready = asyncio.Event(), asyncio.Event()
    @asynccontextmanager
    async def commands(_):
        yield changed
    monkeypatch.setattr(notifications, 'agent_commands', commands)
    writer = SimpleNamespace(emit=AsyncMock(), close=AsyncMock(), heartbeat=AsyncMock(),
        cancel_requested=AsyncMock(return_value=False))
    async def producer(stop):
        ready.set()
        await stop.wait()
        if False:
            yield None
    turn = turn_runtime.new_turn_id()
    buffer, stop = turn_runtime.register_turn(turn)
    task = asyncio.create_task(turn_runtime.run_turn(turn, buffer, stop, producer, writer))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        await asyncio.sleep(0.8)
        assert writer.cancel_requested.await_count == 1
        writer.cancel_requested.return_value = True
        changed.set()
        await asyncio.wait_for(task, 1)
        assert stop.is_set() and writer.cancel_requested.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_event_batches_idle_authorization_and_heartbeat_do_not_read(monkeypatch):
    changed = asyncio.Event()
    @asynccontextmanager
    async def changes(*_):
        yield changed
    monkeypatch.setattr(notifications, 'state_changes', changes)
    read = AsyncMock(return_value=[])
    authorized = AsyncMock(return_value=True)
    stream = notifications.event_batches('test', 'resource', read, authorized,
        heartbeat_seconds=0.02, authorization_seconds=0.01)
    try:
        for _ in range(3):
            assert await asyncio.wait_for(anext(stream), 1) is None
        assert read.await_count == 1
        assert authorized.await_count >= 3
        read.return_value = ['new']
        changed.set()
        assert await asyncio.wait_for(anext(stream), 1) == ['new']
        authorized.return_value = False
        await asyncio.sleep(0.02)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 1)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_two_chat_observers_receive_same_events_and_resume_independently(monkeypatch):
    from dataclasses import dataclass
    @dataclass
    class Event:
        seq: int
        event_type: str
        payload: dict
    watchers = set()
    @asynccontextmanager
    async def changes(_):
        event = asyncio.Event()
        watchers.add(event)
        try:
            yield event
        finally:
            watchers.remove(event)
    @asynccontextmanager
    async def session(**_):
        yield object()
    rows = [Event(1, 'CHAT_EVENT', {'delta': 'first'})]
    run = SimpleNamespace(status='running')
    async def list_events(_run_id, cursor):
        return [row for row in rows if row.seq > cursor]
    repo = SimpleNamespace(get=AsyncMock(return_value=run), list_events=list_events)
    monkeypatch.setattr(notifications, 'agent_changes', changes)
    monkeypatch.setattr(db, 'session_scope', session)
    monkeypatch.setattr(agent_runs_repo, 'AgentRunsRepo', lambda _: repo)
    first = stream.agent_run_event_stream(run_id='shared', after_seq=0, tenant_id='tenant')
    second = stream.agent_run_event_stream(run_id='shared', after_seq=0, tenant_id='tenant')
    resumed = None
    try:
        a, b = await asyncio.gather(anext(first), anext(second))
        assert a == b and b'id: 1' in a
        await second.aclose()
        rows.extend([Event(2, 'CHAT_EVENT', {'delta': 'second'}), Event(3, 'done', {})])
        run.status = 'completed'
        for event in watchers:
            event.set()
        resumed = stream.agent_run_event_stream(run_id='shared', after_seq=1, tenant_id='tenant')
        left = [item async for item in first]
        right = [item async for item in resumed]
        assert left == right
        assert len(left) == 2 and b'id: 2' in left[0] and b'id: 3' in left[1]
    finally:
        await first.aclose()
        await second.aclose()
        if resumed:
            await resumed.aclose()
    assert not watchers


@pytest.mark.asyncio
@pytest.mark.parametrize('after_seq', [0, 255, 256, 257, 1003])
async def test_completed_long_replay_drains_pages_and_preserves_resume_payload(monkeypatch, after_seq):
    """A terminal run still drains every durable page before closing the SSE."""
    from vibecanvas_api.streaming.sse import format_event

    rows = [SimpleNamespace(seq=i, event_type='CHAT_EVENT',
        payload={'delta': f'第{i}段：正文与 emoji 🧪\n', 'index': i})
        for i in range(1, 1003)]
    rows.append(SimpleNamespace(seq=1003, event_type='done', payload={'status': 'completed'}))
    cursors = []
    open_sessions = 0
    watchers = set()

    @asynccontextmanager
    async def changes(_):
        event = asyncio.Event()
        watchers.add(event)
        try:
            yield event
        finally:
            watchers.remove(event)

    @asynccontextmanager
    async def session(**_):
        nonlocal open_sessions
        open_sessions += 1
        try:
            yield object()
        finally:
            open_sessions -= 1

    async def list_events(_, cursor):
        cursors.append(cursor)
        return rows[cursor:cursor + 256]

    repo = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status='completed')),
        list_events=list_events)
    monkeypatch.setattr(notifications, 'agent_changes', changes)
    monkeypatch.setattr(db, 'session_scope', session)
    monkeypatch.setattr(agent_runs_repo, 'AgentRunsRepo', lambda _: repo)
    received = []
    async with asyncio.timeout(2):
        async for item in stream.agent_run_event_stream(
                run_id='long', after_seq=after_seq, tenant_id='tenant'):
            assert open_sessions == 0  # Slow clients must not hold the read session.
            received.append(item)
    assert received == [format_event(row.event_type, row.payload, event_id=row.seq)
        for row in rows[after_seq:]]
    assert cursors[0] == after_seq and cursors[-1] == 1003
    assert len(cursors) == (1003 - after_seq + 255) // 256 + 1
    assert not watchers and open_sessions == 0


@pytest.mark.asyncio
async def test_invalidations_initial_idle_change_and_permission_revocation(monkeypatch):
    changed = asyncio.Event()
    @asynccontextmanager
    async def changes(*_):
        yield changed
    monkeypatch.setattr(notifications, 'state_changes', changes)
    authorized = AsyncMock(return_value=True)
    events = notifications.invalidations('activity', 'chat', authorized,
        heartbeat_seconds=0.02, authorization_seconds=0.01)
    try:
        assert await anext(events) is True
        assert await asyncio.wait_for(anext(events), 1) is False
        changed.set()
        assert await asyncio.wait_for(anext(events), 1) is True
        authorized.return_value = False
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(events), 1)
    finally:
        await events.aclose()
