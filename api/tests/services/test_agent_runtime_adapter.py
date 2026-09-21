from __future__ import annotations

import pytest

from vibecanvas_api.services.agent_runtime.codex_runtime import CodexSandboxRuntime
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeOpenRequest,
    RuntimeTurnRequest,
    RuntimeType,
)


class _FakeSandboxSession:
    def __init__(self):
        self.controls = []
        self.cancelled = []

    async def run_agent_runtime_stream(self, request):
        yield {
            "event_id": "evt_1",
            "seq": 1,
            "chat_id": request["chat_id"],
            "turn_id": request["turn_id"],
            "runtime_type": "codex",
            "runtime_session_id": request["runtime_session_id"],
            "type": "projection",
            "payload": {"event_type": "NO_OP", "payload": {}},
        }

    async def send_agent_runtime_control(self, turn_id, response):
        self.controls.append((turn_id, response))

    async def cancel_agent_runtime(self, turn_id):
        self.cancelled.append(turn_id)
        return True


@pytest.mark.asyncio
async def test_codex_adapter_validates_and_streams_stable_events():
    runtime = CodexSandboxRuntime(_FakeSandboxSession())
    await runtime.open(
        RuntimeOpenRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            runtime_type=RuntimeType.CODEX,
            runtime_session_id="runtime_session",
            runtime_root="/runtime/.codex",
        )
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type=RuntimeType.CODEX,
        runtime_session_id="runtime_session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={"id": "gpt-test", "connection_type": "chatgpt_account"},
    )

    events = [event async for event in runtime.run_turn(request)]
    assert [event.type for event in events] == ["projection"]
    assert events[0].payload["event_type"] == "NO_OP"


@pytest.mark.asyncio
async def test_codex_adapter_delivers_control_and_cancel_on_private_channel():
    sandbox = _FakeSandboxSession()
    runtime = CodexSandboxRuntime(sandbox)
    await runtime.open(
        RuntimeOpenRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            runtime_type=RuntimeType.CODEX,
            runtime_session_id="runtime_session",
            runtime_root="/runtime/.codex",
        )
    )
    from vibecanvas_api.services.agent_runtime.protocol import RuntimeControlResponse

    response = RuntimeControlResponse(
        request_id="hitl",
        chat_id="chat",
        turn_id="turn",
        gate_type="pre_tool_approval",
        action="approve",
        correlation={
            "source": "codex",
            "runtime_request_id": "tool-call",
            "runtime_method": "tool/approval",
        },
    )
    await runtime.respond(response)
    assert await runtime.cancel("turn") is True
    assert sandbox.controls[0][0] == "turn"
    assert sandbox.controls[0][1]["request_id"] == "hitl"
    assert sandbox.cancelled == ["turn"]
