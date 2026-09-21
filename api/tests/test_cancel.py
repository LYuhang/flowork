"""Gate 4 — cancel propagation: request_cancel sets the stop event;
run_turn terminates with an error event coded 'cancelled' within 2s.

Direct unit test against turn_runtime avoids needing a full Runtime process
stack — the cancel contract is between request_cancel + run_turn +
AsyncTurnBuffer, not the agent itself.

The route-level cancel test uses the REAL auth harness (register →
session_token → Bearer) on the conftest async ``client`` fixture (the
legacy ``VIBECANVAS_API_DEV_TOKEN`` Bearer harness is dead).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest


async def _register(client) -> str:
    email = f"cancel_{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


async def _slow_producer(stop):
    for i in range(100):
        if stop.is_set():
            return
        yield "CHAT_UPDATE", {"i": i}
        await asyncio.sleep(0.05)


def test_request_cancel_propagates_to_running_turn():
    from vibecanvas_api.streaming import turn_runtime as rt

    async def main():
        turn_id = rt.new_turn_id()
        buf, stop = rt.register_turn(turn_id)
        runner = asyncio.create_task(
            rt.run_turn(turn_id, buf, stop, _slow_producer)
        )
        # Let the producer emit a few events first.
        await asyncio.sleep(0.15)
        assert rt.request_cancel(turn_id) is True
        await asyncio.wait_for(runner, timeout=2.0)

        # Buffer is closed; subscribe replays the full history.
        events = [e async for e in buf.subscribe()]
        names = [n for n, _ in events]
        assert names[0] == "started"
        assert names[-1] == "error"
        assert events[-1][1].get("code") == "cancelled"

    asyncio.run(main())


@pytest.mark.asyncio
async def test_cancel_unknown_turn_returns_404(client, pg_engine):
    tok = await _register(client)
    r = await client.post(
        "/api/v1/chats/c1/turns/t_unknown/cancel", headers=_hdr(tok),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cancel_chat_without_active_turn_returns_404(client, pg_engine):
    tok = await _register(client)
    r = await client.post(
        "/api/v1/chats/c1/active-turn/cancel", headers=_hdr(tok),
    )
    assert r.status_code == 404
