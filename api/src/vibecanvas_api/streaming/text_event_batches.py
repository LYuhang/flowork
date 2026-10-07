"""Bounded coalescing before durable event sequence IDs are assigned."""
from __future__ import annotations

import asyncio
from contextlib import aclosing


def _text(event):
    name, payload = event
    if name != 'CHAT_EVENT' or not isinstance(payload, dict):
        return None
    if payload.get('type') != 'message_delta' or not payload.get('message_id'):
        return None
    fields = [key for key in ('content', 'delta') if key in payload]
    if len(fields) != 1 or not isinstance(payload[fields[0]], str):
        return None
    field = fields[0]
    return field, {key: value for key, value in payload.items() if key != field}


async def coalesce_text_events(source, *, window_seconds=0.04, max_bytes=8192):
    """Merge adjacent matching text fragments; boundaries retain exact order.

    The producer stays on one task so its context variables survive yields.
    A bounded queue applies backpressure; only one bounded text batch is held.
    All emitted batches still pass through durable persistence before SSE.
    """
    queue = asyncio.Queue(maxsize=16)
    end = object()
    consumer_closed = False

    async def produce():
        try:
            async with aclosing(source):
                async for event in source:
                    await queue.put(event)
        except asyncio.CancelledError as error:
            # A runtime cancellation must wake the consumer too. During our
            # own cleanup there is no consumer left, so never enqueue then.
            if not consumer_closed:
                await queue.put(error)
        except Exception as error:
            await queue.put(error)
        finally:
            # Cancellation during cleanup must not block on a full queue.
            if not asyncio.current_task().cancelling():
                await queue.put(end)

    producer = asyncio.create_task(produce())
    buffered = None
    try:
        while True:
            event = buffered if buffered is not None else await queue.get()
            buffered = None
            if event is end:
                return
            if isinstance(event, BaseException):
                raise event
            signature = _text(event)
            if signature is None:
                yield event
                continue
            field, _metadata = signature
            payload = dict(event[1])
            size = len(payload[field].encode('utf-8'))
            deadline = asyncio.get_running_loop().time() + window_seconds
            while size < max_bytes:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    following = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if following is end or isinstance(following, BaseException) or _text(following) != signature:
                    buffered = following
                    break
                text = following[1][field]
                added = len(text.encode('utf-8'))
                if size + added > max_bytes:
                    buffered = following
                    break
                payload[field] += text
                size += added
            yield event[0], payload
    finally:
        consumer_closed = True
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
