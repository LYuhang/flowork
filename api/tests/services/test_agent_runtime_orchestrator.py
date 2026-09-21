from __future__ import annotations

import asyncio

import pytest

from vibecanvas_api.services.agent_runtime.orchestrator import (
    AgentRuntimeOrchestrator,
    _product_events,
    private_runtime_root,
)
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeEvent,
    RuntimeOpenRequest,
    RuntimeTurnRequest,
    RuntimeType,
)


class _Sandbox:
    async def run_agent_runtime_stream(self, request):
        common = {
            "chat_id": request["chat_id"],
            "turn_id": request["turn_id"],
            "runtime_type": "codex",
            "runtime_session_id": request["runtime_session_id"],
        }
        yield {**common, "event_id": "start", "seq": 1, "type": "runtime.started"}
        yield {
            **common,
            "event_id": "projection",
            "seq": 2,
            "type": "projection",
            "payload": {
                "event_type": "CHAT_EVENT",
                "payload": {"type": "message_replace", "content": "hello"},
            },
        }
        yield {**common, "event_id": "done", "seq": 3, "type": "runtime.completed"}

    async def cancel_agent_runtime(self, _turn_id):
        return True

    async def send_agent_runtime_control(self, _turn_id, _response):
        return None


class _Manager:
    def __init__(self):
        self.session = _Sandbox()
        self.calls = []

    async def get_session(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.session


def test_private_runtime_root_is_codex_owned() -> None:
    assert private_runtime_root(RuntimeType.CODEX, "chat/unsafe") == "/runtime/.codex"


def test_interaction_required_projects_runtime_neutral_waiting_state() -> None:
    projection = {
        "type": "tool_update",
        "tool_call_id": "input-1",
        "status": "running",
        "artifact": {"payload": {"kind": "interactive_artifact"}},
    }
    event = RuntimeEvent(
        event_id="event-1",
        seq=1,
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime",
        type="interaction.required",
        payload={
            "hitl_request_id": "hitl-input-1",
            "resume_mode": "same_turn",
            "projection_event": projection,
        },
    )
    assert _product_events(event) == [
        (
            "INTERACTION_REQUIRED",
            {
                "hitl_request_id": "hitl-input-1",
                "resume_mode": "same_turn",
                "projection_event": projection,
            },
        ),
        ("CHAT_EVENT", projection),
    ]


@pytest.mark.asyncio
async def test_orchestrator_streams_codex_through_resident_sandbox() -> None:
    manager = _Manager()
    orchestrator = AgentRuntimeOrchestrator(manager)
    open_request = RuntimeOpenRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        runtime_type="codex",
        runtime_session_id="runtime",
        runtime_root="/runtime/.codex",
    )
    turn_request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={"id": "gpt-test", "connection_type": "chatgpt_account"},
    )
    events = [
        event
        async for event in orchestrator.stream_turn(
            open_request=open_request,
            turn_request=turn_request,
            workspace_scope_id="workspace",
            current_workflow_id=None,
            stop_event=asyncio.Event(),
        )
    ]
    assert events[-1] == (
        "CHAT_EVENT",
        {"type": "message_replace", "content": "hello"},
    )
    assert manager.calls
