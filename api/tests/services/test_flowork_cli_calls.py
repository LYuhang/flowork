"""Ordinary CLI operations are turn-owned jobs, not 50-second RPC bodies."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.agent_runtime import cli_calls, cli_host


@pytest.mark.asyncio
async def test_long_command_survives_repeated_control_polls_and_returns_once(monkeypatch):
    cap = SimpleNamespace(tenant_id="tenant", user_id="user", chat_id="chat", turn_id="turn", runtime_session_id="runtime")
    monkeypatch.setattr(cli_calls.agent_context, "resolve_context", AsyncMock(return_value=object()))
    clock = [0.0]
    monkeypatch.setattr(cli_calls, "monotonic", lambda: clock[0])
    release = asyncio.Event()
    async def business(**kwargs):
        await release.wait()
        return {"workflows": []}
    invoke = AsyncMock(side_effect=business)
    monkeypatch.setattr(cli_host, "invoke_workflow_command", invoke)
    call_id = "a" * 32
    try:
        assert await cli_calls.command(cap, "cli.start", {"call_id": call_id, "operation": "workflow.list", "arguments": {}}, "token") == {"started": True}
        await asyncio.sleep(0)
        # Elapsed business duration exceeds both retired implicit limits.
        for now in range(0, 1000, 10):
            clock[0] = float(now)
            result = await cli_calls.command(cap, "cli.poll", {"call_id": call_id, "ack": 0}, "token")
            assert result["event"] is None
        release.set()
        await asyncio.sleep(0)
        result = await cli_calls.command(cap, "cli.poll", {"call_id": call_id, "ack": 0}, "token")
        assert result["event"]["terminal"] is True
        assert result["event"]["result"] == {"workflows": []}
        replay = await cli_calls.command(cap, "cli.poll", {"call_id": call_id, "ack": 0}, "token")
        assert replay == result
        invoke.assert_awaited_once()
    finally:
        await cli_calls.cancel_turn_calls("tenant", "chat", "turn")
    assert not cli_calls._calls


@pytest.mark.asyncio
async def test_turn_end_cancels_pending_job_and_never_resumes_in_next_turn(monkeypatch):
    cap = SimpleNamespace(tenant_id="tenant", user_id="user", chat_id="chat", turn_id="turn", runtime_session_id="runtime")
    monkeypatch.setattr(cli_calls.agent_context, "resolve_context", AsyncMock(return_value=object()))
    stopped = asyncio.Event()
    async def business(**kwargs):
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    monkeypatch.setattr(cli_host, "invoke_workflow_command", business)
    call_id = "b" * 32
    await cli_calls.command(cap, "cli.start", {"call_id": call_id, "operation": "workflow.list", "arguments": {}}, "token")
    await asyncio.sleep(0)
    await cli_calls.cancel_turn_calls("tenant", "chat", "turn")
    assert stopped.is_set()
    cap.turn_id = "next"
    result = await cli_calls.command(cap, "cli.poll", {"call_id": call_id, "ack": 0}, "token")
    assert result["error"] == "result_unknown"
