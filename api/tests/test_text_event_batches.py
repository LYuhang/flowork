import asyncio
from contextlib import aclosing

import pytest

from vibecanvas_api.streaming.text_event_batches import coalesce_text_events


def delta(value, message='m'):
    return 'CHAT_EVENT', {'type': 'message_delta', 'message_id': message, 'delta': value}


@pytest.mark.asyncio
async def test_high_frequency_text_is_complete_bounded_and_ordered():
    tokens = ['数' + str(i) for i in range(500)]
    boundary = ('CHAT_EVENT', {'type': 'tool_start', 'name': 'test'})
    async def source():
        for token in tokens:
            yield delta(token)
        yield boundary
        yield delta('tail', 'other')
    rows = [row async for row in coalesce_text_events(source(), max_bytes=256)]
    assert ''.join(row[1]['delta'] for row in rows[:-2]) == ''.join(tokens)
    assert all(len(row[1]['delta'].encode()) <= 256 for row in rows[:-2])
    assert len(rows) < 30
    assert rows[-2:] == [boundary, delta('tail', 'other')]


@pytest.mark.asyncio
async def test_silent_source_does_not_hold_pending_text():
    closed = asyncio.Event()
    async def source():
        try:
            yield delta('visible')
            await asyncio.Event().wait()
        finally:
            closed.set()
    async with aclosing(coalesce_text_events(source(), window_seconds=0.01)) as stream:
        assert await asyncio.wait_for(anext(stream), 1) == delta('visible')
    assert closed.is_set()


@pytest.mark.asyncio
async def test_error_flushes_preceding_text_then_propagates():
    async def source():
        yield delta('before')
        raise ValueError('broken')
    async with aclosing(coalesce_text_events(source())) as stream:
        assert await anext(stream) == delta('before')
        with pytest.raises(ValueError, match='broken'):
            await anext(stream)


@pytest.mark.asyncio
async def test_slow_consumer_backpressures_source_and_cleanup_does_not_hang():
    produced = 0
    closed = asyncio.Event()
    async def source():
        nonlocal produced
        try:
            for i in range(10000):
                produced += 1
                yield 'event', {'i': i}
        finally:
            closed.set()
    async with aclosing(coalesce_text_events(source())) as stream:
        assert await anext(stream) == ('event', {'i': 0})
        await asyncio.sleep(0.02)
        assert produced <= 18
    assert closed.is_set()


@pytest.mark.asyncio
async def test_cancelled_source_does_not_leave_consumer_waiting_forever():
    async def source():
        yield 'event', {'started': True}
        asyncio.current_task().cancel()
        await asyncio.sleep(0)
    async with aclosing(coalesce_text_events(source())) as stream:
        assert await anext(stream) == ('event', {'started': True})
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(anext(stream), 0.2)
