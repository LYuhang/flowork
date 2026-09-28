"""turn_runtime: register, run a producer, drain via SSE, cancel + GC."""

from __future__ import annotations

import asyncio

import pytest

from vibecanvas_api.streaming import turn_runtime as rt


@pytest.fixture(autouse=True)
def reset_registries():
    rt.TURN_BUFFERS.clear()
    rt.TURN_TASKS.clear()
    rt.TURN_STOP.clear()
    rt.ACTIVE_TURN_BY_CHAT.clear()
    rt.TURN_FINISHED_AT.clear()
    yield
    rt.TURN_BUFFERS.clear()
    rt.TURN_TASKS.clear()
    rt.TURN_STOP.clear()
    rt.ACTIVE_TURN_BY_CHAT.clear()
    rt.TURN_FINISHED_AT.clear()


async def test_run_turn_drains_producer_with_started_done_fence():
    async def producer(stop):
        yield "CHAT_UPDATE", {"i": 0}
        yield "CHAT_UPDATE", {"i": 1}

    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    await rt.run_turn(turn_id, buf, stop, producer)

    events = [e async for e in buf.subscribe()]
    names = [e[0] for e in events]
    assert names[0] == "started"
    assert "CHAT_UPDATE" in names
    assert names[-1] == "done"


async def test_run_turn_emits_error_on_exception():
    async def producer(stop):
        yield "CHAT_UPDATE", {"i": 0}
        raise RuntimeError("boom")

    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    await rt.run_turn(turn_id, buf, stop, producer)
    events = [e async for e in buf.subscribe()]
    assert events[-1][0] == "error"
    assert "boom" in events[-1][1]["message"]


async def test_run_turn_persists_same_sequence_ids_as_sse_buffer():
    class Writer:
        def __init__(self):
            self.events = []
            self.close_count = 0

        async def emit(self, seq, event_type, payload):
            if event_type in {"done", "error"}:
                assert self.close_count == 1, "Terminal consumers must see the flushed transcript"
            self.events.append((seq, event_type, payload))

        async def heartbeat(self):
            return None

        async def cancel_requested(self):
            return False

        async def close(self):
            self.close_count += 1

    async def producer(stop):
        yield "CHAT_UPDATE", {"i": 0}

    writer = Writer()
    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    await rt.run_turn(turn_id, buf, stop, producer, durable_writer=writer)

    assert [seq for seq, _name, _payload in writer.events] == [1, 2, 3]
    assert writer.close_count == 1
    assert [name for _seq, name, _payload in writer.events] == [
        "started", "CHAT_UPDATE", "done",
    ]


async def test_run_turn_never_publishes_an_event_before_durable_emit_finishes():
    persisted: list[str] = []
    release = asyncio.Event()

    class Writer:
        async def emit(self, _seq, event_type, _payload):
            if event_type == "CHAT_UPDATE":
                await release.wait()
            persisted.append(event_type)

        async def heartbeat(self):
            return None

        async def cancel_requested(self):
            return False

        async def close(self):
            return None

    async def producer(_stop):
        yield "CHAT_UPDATE", {"content": "durable first"}

    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    runner = asyncio.create_task(rt.run_turn(turn_id, buf, stop, producer, Writer()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert buf.event_count == 1
    assert persisted == ["started"]
    release.set()
    await runner
    assert persisted == ["started", "CHAT_UPDATE", "done"]


async def test_request_cancel_terminates_with_cancelled_error():
    async def producer(stop):
        for i in range(100):
            if stop.is_set():
                return
            yield "CHAT_UPDATE", {"i": i}
            await asyncio.sleep(0.01)

    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    runner = asyncio.create_task(rt.run_turn(turn_id, buf, stop, producer))
    await asyncio.sleep(0.05)
    assert rt.request_cancel(turn_id) is True
    await runner
    events = [e async for e in buf.subscribe()]
    assert any(e[0] == "error" and e[1].get("code") == "cancelled"
               for e in events)


async def test_cancel_drains_backend_closure_events_before_terminal_cancel():
    async def producer(stop):
        yield "CHAT_EVENT", {"type": "tool_start", "tool_call_id": "tc1"}
        while not stop.is_set():
            await asyncio.sleep(0.01)
        yield "CHAT_EVENT", {
            "type": "tool_end",
            "tool_call_id": "tc1",
            "content": "Tool call cancelled by user.",
            "status": "error",
        }

    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    runner = asyncio.create_task(rt.run_turn(turn_id, buf, stop, producer))
    await asyncio.sleep(0.03)
    assert rt.request_cancel(turn_id) is True
    await runner
    events = [e async for e in buf.subscribe()]
    assert ("CHAT_EVENT", {
        "type": "tool_end",
        "tool_call_id": "tc1",
        "content": "Tool call cancelled by user.",
        "status": "error",
    }) in events
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "cancelled"


async def test_request_cancel_signals_registered_turn_and_emits_terminal():
    async def producer(stop):
        yield "CHAT_EVENT", {"type": "tool_start", "tool_call_id": "tc1"}
        while not stop.is_set():
            await asyncio.sleep(0.01)
        yield "CHAT_EVENT", {
            "type": "tool_end",
            "tool_call_id": "tc1",
            "content": "Tool call cancelled by user.",
            "status": "error",
        }

    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    runner = asyncio.create_task(rt.run_turn(turn_id, buf, stop, producer))
    rt.TURN_TASKS[turn_id] = runner
    await asyncio.sleep(0.03)

    assert rt.request_cancel(turn_id) is True
    await runner

    events = [e async for e in buf.subscribe()]
    assert ("CHAT_EVENT", {
        "type": "tool_end",
        "tool_call_id": "tc1",
        "content": "Tool call cancelled by user.",
        "status": "error",
    }) in events
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "cancelled"


def test_request_cancel_for_chat_only_cancels_matching_active_turn():
    turn_a = rt.new_turn_id()
    _buf_a, stop_a = rt.register_turn(turn_a)
    turn_b = rt.new_turn_id()
    _buf_b, stop_b = rt.register_turn(turn_b)
    rt.mark_chat_active("chat_a", turn_a)
    rt.mark_chat_active("chat_b", turn_b)

    assert rt.request_cancel_for_chat("chat_a", turn_b) is False
    assert not stop_a.is_set()
    assert not stop_b.is_set()

    assert rt.request_cancel_for_chat("chat_a", turn_a) is True
    assert stop_a.is_set()
    assert not stop_b.is_set()


async def test_stream_buffer_as_sse_produces_wire_format():
    async def producer(stop):
        yield "CHAT_UPDATE", {"i": 0}

    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    await rt.run_turn(turn_id, buf, stop, producer)
    chunks = []
    async for chunk in rt.stream_buffer_as_sse(turn_id):
        chunks.append(chunk)
    all_bytes = b"".join(chunks)
    assert b"id: 1\n" in all_bytes
    assert b"event: started\n" in all_bytes
    assert b"event: CHAT_UPDATE\n" in all_bytes
    assert b"event: done\n" in all_bytes


async def test_gc_sweep_removes_expired_buffers(monkeypatch):
    monkeypatch.setattr(rt, "_GC_AFTER_SECONDS", 0.05)

    async def producer(stop):
        return
        yield  # unreachable

    turn_id = rt.new_turn_id()
    buf, stop = rt.register_turn(turn_id)
    await rt.run_turn(turn_id, buf, stop, producer)
    assert turn_id in rt.TURN_BUFFERS

    await asyncio.sleep(0.1)
    removed = await rt.gc_sweep_once()
    assert removed == 1
    assert turn_id not in rt.TURN_BUFFERS
