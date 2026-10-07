"""Real PostgreSQL disconnect/reconnect and shared listener lifecycle."""
import asyncio
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest

from vibecanvas_api.services import state_notifications as notifications


@pytest.mark.asyncio
async def test_shared_listener_reconnect_wakes_all_and_last_subscriber_closes(pg_engine, monkeypatch):
    original_connect = notifications.asyncpg.connect
    connections = []
    reconnect_entered = asyncio.Event()
    permit_reconnect = asyncio.Event()

    async def connect(*args, **kwargs):
        if connections:
            reconnect_entered.set()
            await permit_reconnect.wait()
        connection = await original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(notifications.asyncpg, 'connect', connect)
    channel = 'qa_listener_' + uuid4().hex
    key = (asyncio.get_running_loop(), channel)
    async with AsyncExitStack() as outer:
        a = await outer.enter_async_context(notifications.state_changes(channel, 'a'))
        async with AsyncExitStack() as inner:
            second_a = await inner.enter_async_context(notifications.state_changes(channel, 'a'))
            b = await inner.enter_async_context(notifications.state_changes(channel, 'b'))
            for event in (a, second_a, b):
                await asyncio.wait_for(event.wait(), 5)
                event.clear()
            assert len(connections) == 1
            # A real PostgreSQL notification fans out by resource, not by consumer.
            await connections[0].execute('SELECT pg_notify($1, $2)', channel, 'a')
            await asyncio.wait_for(asyncio.gather(a.wait(), second_a.wait()), 5)
            assert not b.is_set()
            a.clear()
            second_a.clear()
            connections[0].terminate()
            await asyncio.wait_for(reconnect_entered.wait(), 5)
            assert not any(event.is_set() for event in (a, second_a, b))
            permit_reconnect.set()
            await asyncio.wait_for(asyncio.gather(a.wait(), second_a.wait(), b.wait()), 5)
            assert len(connections) == 2
            assert connections[0].is_closed()
            assert not connections[1].is_closed()
        assert not connections[1].is_closed()  # One subscriber still owns it.
        assert set(notifications._listeners[key].subscribers) == {'a'}
    assert key not in notifications._listeners
    assert connections[1].is_closed()
