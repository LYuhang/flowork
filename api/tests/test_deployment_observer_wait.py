"""HTTP observations wait on commit notifications, never periodic state polls."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from vibecanvas_api.services import state_notifications as notifications


@pytest.mark.asyncio
async def test_waiters_share_one_listener_and_cleanup_after_last(monkeypatch):
    starts = []
    async def listen(self):
        starts.append(self)
        await asyncio.Event().wait()
    monkeypatch.setattr(notifications._Listener, 'listen', listen)
    async with notifications.execution_changes('a') as first:
        async with notifications.execution_changes('a') as second:
            async with notifications.execution_changes('b') as other:
                await asyncio.sleep(0)
                assert len(starts) == 1
                starts[0].wake('a')
                assert first.is_set() and second.is_set() and not other.is_set()
                starts[0].wake()
                assert other.is_set()
        assert not starts[0].task.done()
    assert starts[0].task.cancelled()
    assert not notifications._listeners


@pytest.mark.asyncio
@pytest.mark.parametrize('concurrency', [1, 8])
async def test_observer_does_not_query_again_without_event(monkeypatch, concurrency):
    from vibecanvas_api.services import deployment_observer as observer
    from types import SimpleNamespace
    changed = asyncio.Event()
    @asynccontextmanager
    async def changes(_):
        yield changed
    @asynccontextmanager
    async def session_scope(**kwargs):
        yield session
    session = SimpleNamespace(scalar=AsyncMock(return_value=False), execute=AsyncMock(return_value=Mock()))
    session.execute.return_value.mappings.return_value.one_or_none.return_value = None
    history = SimpleNamespace(get=AsyncMock(return_value={'status': 'running'}))
    monkeypatch.setattr(notifications, 'execution_changes', changes)
    monkeypatch.setattr(observer, 'session_scope', session_scope)
    monkeypatch.setattr(observer, 'WorkflowHistoryRepo', lambda _: history)
    tasks = [asyncio.create_task(observer.observe_invocation(tenant_id='t', slug='s',
        invocation_id=f'00000000-0000-0000-0000-{index + 1:012d}')) for index in range(concurrency)]
    try:
        await asyncio.sleep(1.1)
        assert history.get.await_count == concurrency
        changed.set()
        await asyncio.sleep(0.01)
        assert history.get.await_count == 2 * concurrency
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)


@pytest.mark.asyncio
async def test_notifications_are_commit_bound_and_reconnect_wakes(pg_engine):
    from sqlalchemy import text
    from vibecanvas_api.storage.db import session_scope
    async with notifications.execution_changes('test-execution') as changed:
        await asyncio.wait_for(changed.wait(), 5)
        changed.clear()
        async with session_scope() as session:
            await session.execute(text("SELECT pg_notify('flowork_execution_state', 'test-execution')"))
            await asyncio.sleep(0.02)
            assert not changed.is_set()
        await asyncio.wait_for(changed.wait(), 5)
        changed.clear()
        async with pg_engine.connect() as connection:
            pids = (await connection.execute(text(
                "SELECT pid FROM pg_stat_activity WHERE application_name='flowork-state-listener' AND datname=current_database()"
            ))).scalars().all()
            assert len(pids) == 1
            await connection.execute(text('SELECT pg_terminate_backend(:pid)'), {'pid': pids[0]})
        await asyncio.wait_for(changed.wait(), 8)
    assert not notifications._listeners


@pytest.mark.asyncio
async def test_listener_cleanup_does_not_swallow_callers_cancellation(monkeypatch):
    ready = asyncio.Event()
    leave = asyncio.Event()
    closing = asyncio.Event()
    async def listen(self):
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            closing.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(notifications._Listener, 'listen', listen)
    async def use_listener():
        async with notifications.execution_changes('cleanup'):
            await leave.wait()
    task = asyncio.create_task(use_listener())
    await asyncio.wait_for(ready.wait(), 1)
    leave.set()
    await asyncio.wait_for(closing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert not notifications._listeners


@pytest.mark.asyncio
async def test_notification_racing_last_subscriber_exit_releases_connection(pg_engine):
    from sqlalchemy import text
    from vibecanvas_api.storage.db import session_scope

    for _ in range(10):
        async with asyncio.timeout(3):
            async with notifications.execution_changes('closing') as changed:
                await changed.wait()
                async with session_scope() as session:
                    await session.execute(text("SELECT pg_notify('flowork_execution_state', 'closing')"))
                # Deliberately leave while the committed callback may be queued.
        assert not notifications._listeners
    async with pg_engine.connect() as connection:
        count = await connection.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name='flowork-state-listener' AND datname=current_database()"
        ))
    assert count == 0
