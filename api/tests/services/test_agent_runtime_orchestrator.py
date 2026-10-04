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
    assert private_runtime_root(RuntimeType.CODEX) == "/runtime/.codex"


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
@pytest.mark.parametrize("shared_workflow", [False, True])
async def test_orchestrator_streams_codex_through_resident_sandbox(shared_workflow) -> None:
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
    from vibecanvas_api.services.sandbox.contracts import WorkflowRunSource
    source = WorkflowRunSource(tenant_id="owner-tenant", workflow_id="shared-workflow") if shared_workflow else None
    turn_request = RuntimeTurnRequest(
        workflow_run_source=source,
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
            stop_event=asyncio.Event(),
        )
    ]
    assert events[-1] == (
        "CHAT_EVENT",
        {"type": "message_replace", "content": "hello"},
    )
    assert manager.calls
    assert manager.calls[0][0][1] == "workspace"
    assert manager.calls[0][1]["lease"] == "interactive"
    assert manager.calls[0][1].get("workflow_run_source") == source
    assert manager.calls[0][0][0] == "tenant"
    assert manager.calls[0][0][2] == "user"


@pytest.mark.asyncio
async def test_runtime_input_is_persisted_privately_before_public_events(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from vibecanvas_api.services.agent_runtime import orchestrator as module

    payload = {"input": [{"type": "text", "text": "private command context"}]}
    record = AsyncMock()
    session_marker = object()

    @asynccontextmanager
    async def scope(**identity):
        assert identity == {"tenant_id": "tenant", "user_id": "user"}
        yield session_marker

    class Repo:
        def __init__(self, session):
            assert session is session_marker

        record_runtime_input = staticmethod(record)

    class Sandbox(_Sandbox):
        async def run_agent_runtime_stream(self, request):
            yield {
                "chat_id": request["chat_id"], "turn_id": request["turn_id"],
                "runtime_type": "codex", "runtime_session_id": "runtime",
                "event_id": "private-input", "seq": 1,
                "type": "runtime.input", "payload": payload,
            }
            async for event in super().run_agent_runtime_stream(request):
                yield {**event, "seq": event["seq"] + 1}

    monkeypatch.setattr(module, "session_scope", scope)
    monkeypatch.setattr(module, "AgentRunsRepo", Repo)
    manager = _Manager()
    manager.session = Sandbox()
    common = dict(tenant_id="tenant", user_id="user", chat_id="chat",
                  runtime_type="codex", runtime_session_id="runtime",
                  runtime_root="/runtime/.codex")
    events = [event async for event in AgentRuntimeOrchestrator(manager).stream_turn(
        open_request=RuntimeOpenRequest(**common),
        turn_request=RuntimeTurnRequest(**common, turn_id="turn",
            message={"role": "user", "content": "hello"},
            model={"id": "gpt-test", "connection_type": "chatgpt_account"}),
        workspace_scope_id="workspace", stop_event=asyncio.Event(),
    )]
    record.assert_awaited_once_with(
        "turn", chat_id="chat", creator_user_id="user",
        event_id="private-input", payload=payload,
    )
    assert "private command context" not in str(events)
    assert events[-1][0] == "CHAT_EVENT"
