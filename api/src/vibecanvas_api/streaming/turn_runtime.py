"""Per-turn registries + runner + GC sweep.

Holds three dicts keyed by turn_id (or exec_id; same string-id space):

    TURN_BUFFERS: AsyncTurnBuffer per in-flight turn
    TURN_TASKS:   the asyncio.Task running the agent / exec
    TURN_STOP:    asyncio.Event clients use to signal cancel

Spec §3.3 — buffers GC'd 30 minutes after the runner completes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, AsyncIterator, Callable

import structlog

from .async_turn_buffer import AsyncTurnBuffer
from .sse import format_event

logger = structlog.get_logger(__name__)

TURN_BUFFERS: dict[str, AsyncTurnBuffer] = {}
TURN_TASKS:   dict[str, asyncio.Task] = {}
TURN_STOP:    dict[str, asyncio.Event] = {}
TURN_FINISHED_AT: dict[str, float] = {}   # ts when runner completed
_GC_AFTER_SECONDS = 1800  # 30 minutes

# chat_id -> turn_id of the chat's currently-active turn. TURN_TASKS is keyed by
# turn_id, so it can't answer "is THIS chat busy?"; the background-task watcher
# needs that to decide whether to fire a callback turn now or defer it (B3).
ACTIVE_TURN_BY_CHAT: dict[str, str] = {}


def mark_chat_active(chat_id: str, turn_id: str) -> None:
    if chat_id:
        ACTIVE_TURN_BY_CHAT[chat_id] = turn_id


def clear_chat_active(chat_id: str, turn_id: str) -> None:
    # Only clear if THIS turn still owns the slot (a newer turn may have replaced it).
    if ACTIVE_TURN_BY_CHAT.get(chat_id) == turn_id:
        ACTIVE_TURN_BY_CHAT.pop(chat_id, None)


def is_chat_busy(chat_id: str) -> bool:
    return chat_id in ACTIVE_TURN_BY_CHAT


def new_turn_id() -> str:
    # Durable rows outlive one process, so use the full UUID entropy. The old
    # 12-hex (48-bit) id was acceptable only for an in-memory runtime registry.
    return f"t_{uuid.uuid4().hex}"


def register_turn(
    turn_id: str, *, drop_oldest: bool = False,
) -> tuple[AsyncTurnBuffer, asyncio.Event]:
    """Register a new turn buffer + stop event.

    With ``drop_oldest``, the chat path keeps the default
    (overflow RAISES — chat is resumable, the head must not be lost). The
    EXEC path passes ``drop_oldest=True`` so a high-frame run (a large
    loop emitting running+success per node per iteration) evicts the
    oldest frames at the cap instead of falsely failing the run.
    """
    buf = AsyncTurnBuffer(drop_oldest=drop_oldest)
    stop = asyncio.Event()
    TURN_BUFFERS[turn_id] = buf
    TURN_STOP[turn_id] = stop
    return buf, stop


def get_buffer(turn_id: str) -> AsyncTurnBuffer | None:
    return TURN_BUFFERS.get(turn_id)


def request_cancel(turn_id: str) -> bool:
    ev = TURN_STOP.get(turn_id)
    if ev is None:
        return False
    ev.set()
    return True


def request_cancel_for_chat(chat_id: str, turn_id: str) -> bool:
    """Cancel only when the turn is still the active turn for this chat.

    A turn id is globally unique, but the user-facing Stop action is scoped to a
    chat row. Keep that invariant at the registry boundary so a stale UI cannot
    cancel an unrelated chat's in-flight turn by accidentally reusing a turn id.
    """
    if not chat_id or ACTIVE_TURN_BY_CHAT.get(chat_id) != turn_id:
        return False
    return request_cancel(turn_id)


async def run_turn(
    turn_id: str,
    buffer: AsyncTurnBuffer,
    stop: asyncio.Event,
    producer: Callable[[asyncio.Event], AsyncIterator[tuple[str, Any]]],
    durable_writer: Any | None = None,
) -> None:
    """Drain ``producer`` events into ``buffer`` until completion / cancel /
    error. ``producer`` yields ``(event_name, payload)`` pairs.
    """
    turn_started = time.perf_counter()
    helper_tasks: list[asyncio.Task] = []
    transcript_closed = False

    async def _emit(event_name: str, payload: Any) -> None:
        nonlocal transcript_closed
        if durable_writer is not None and event_name in {"done", "error"} and not transcript_closed:
            # Terminal consumers reload the database immediately. Flush the
            # already-buffered interrupted message before advertising that
            # boundary, not after it. No snapshot generation runs here.
            await durable_writer.close()
            transcript_closed = True
        prepare = None
        if durable_writer is not None:
            async def prepare(seq: int) -> None:
                await durable_writer.emit(seq, event_name, payload)
        await buffer.put_after_prepare((event_name, payload), prepare)

    async def _heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(10.0)
            await durable_writer.heartbeat()

    async def _cancel_watch_loop() -> None:
        while True:
            await asyncio.sleep(0.75)
            if await durable_writer.cancel_requested():
                stop.set()
                return

    if durable_writer is not None:
        helper_tasks = [
            asyncio.create_task(_heartbeat_loop()),
            asyncio.create_task(_cancel_watch_loop()),
        ]

    try:
        # Always emit a 'started' frame first.
        await _emit("started", {"turn_id": turn_id})
        first_producer_event = True
        async for event_name, payload in producer(stop):
            if first_producer_event:
                first_producer_event = False
                logger.info(
                    "agent_turn_timing",
                    phase="first_producer_event",
                    event_type=event_name,
                    turn_id=turn_id,
                    elapsed_ms=int((time.perf_counter() - turn_started) * 1000),
                )
            await _emit(event_name, payload)
        if stop.is_set():
            await _emit("error", {"code": "cancelled",
                                  "message": "Turn cancelled by client."})
        else:
            await _emit("done", {})
    except asyncio.CancelledError:
        await _emit("error", {"code": "cancelled",
                              "message": "Turn cancelled by client."})
    except Exception as e:
        await _emit("error", {"code": "engine_error", "message": str(e)})
    finally:
        for task in helper_tasks:
            task.cancel()
        for task in helper_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if durable_writer is not None and not transcript_closed:
            await durable_writer.close()
        logger.info(
            "agent_turn_timing",
            phase="runner_total",
            turn_id=turn_id,
            elapsed_ms=int((time.perf_counter() - turn_started) * 1000),
            cancelled=stop.is_set(),
            durable=durable_writer is not None,
        )
        await buffer.close()
        TURN_FINISHED_AT[turn_id] = time.time()
        # Clear chat-active tracking so the bg-task watcher can fire an idle
        # callback turn. run_turn only has the turn_id, so reverse-lookup the chat.
        for _cid, _tid in list(ACTIVE_TURN_BY_CHAT.items()):
            if _tid == turn_id:
                ACTIVE_TURN_BY_CHAT.pop(_cid, None)
        # Every Turn finalize is a low-latency drain trigger. PostgreSQL still
        # owns the pending set, so a missed process-local wake is harmless.
        try:
            from vibecanvas_api.services.background_delivery import (
                background_result_delivery,
            )
            background_result_delivery.notify()
        except Exception:
            pass


async def stream_buffer_as_sse(turn_id: str, *, after_seq: int = 0) -> AsyncIterator[bytes]:
    """Subscribe to the buffer and yield SSE-encoded bytes."""
    buf = TURN_BUFFERS.get(turn_id)
    if buf is None:
        return
    async for seq, event in buf.subscribe_with_ids(15.0, after_seq=after_seq):
        event_name, payload = event
        yield format_event(event_name, payload, event_id=seq)


def unwrap_signal(ev: Any) -> tuple[str, dict]:
    """Translate a ``build_signal`` envelope to a ``(NAME, payload)`` tuple.

    ``run_agent_turn`` still yields legacy-shaped signal envelopes
    ``{"__signal_id__": ..., "type": NAME, "payload": {...}}`` — the
    chats route adapts them to the ``(event_name, payload)`` shape that
    ``run_turn`` expects. Tuples are passed through unchanged so this
    helper is safe to layer over any producer.
    """
    if isinstance(ev, dict) and "type" in ev:
        return (ev.get("type"), ev.get("payload") or {})
    return ev


async def gc_sweep_once() -> int:
    """One pass of buffer GC. Returns count of buffers removed."""
    now = time.time()
    expired = [
        tid for tid, fin in TURN_FINISHED_AT.items()
        if now - fin >= _GC_AFTER_SECONDS
    ]
    for tid in expired:
        TURN_BUFFERS.pop(tid, None)
        TURN_TASKS.pop(tid, None)
        TURN_STOP.pop(tid, None)
        TURN_FINISHED_AT.pop(tid, None)
    return len(expired)
