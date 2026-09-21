from __future__ import annotations

import os
import subprocess
import sys

import pytest
from pydantic import ValidationError
from vibecanvas_engine.sandbox_bus import MSG_RUNTIME_REQUEST

from vibecanvas_api.services.agent_runtime import (
    RuntimeControlResponse,
    RuntimeEvent,
    RuntimeOpenRequest,
    RuntimeTurnRequest,
)
from vibecanvas_api.services.agent_runtime.protocol import RuntimeSkill


@pytest.mark.asyncio
async def test_sandbox_entry_accepts_consecutive_runtime_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The main-agent process stays on its bus after one completed turn."""
    from vibecanvas_api.services.agent_runtime import sandbox_entry

    def request(turn_id: str) -> dict:
        return {
            "tenant_id": "tenant",
            "user_id": "user",
            "chat_id": "chat",
            "turn_id": turn_id,
            "runtime_type": "codex",
            "runtime_session_id": "session",
            "runtime_root": "/runtime/.codex",
            "runtime_state_ref": "thread",
            "message": {"role": "user", "content": turn_id},
            "model": {"id": "gpt-test", "connection_type": "chatgpt_account"},
        }

    class FakeChannel:
        def __init__(self) -> None:
            self.incoming = [
                {"type": MSG_RUNTIME_REQUEST, "request": request("turn-1")},
                {"type": MSG_RUNTIME_REQUEST, "request": request("turn-2")},
                None,
            ]
            self.closed = False
            self.sent: list[dict] = []

        async def recv(self):
            return self.incoming.pop(0)

        async def send(self, message):
            self.sent.append(message)

        async def close(self):
            self.closed = True

    channel = FakeChannel()
    seen: list[str] = []

    async def fake_connect_bus(_socket_path: str):
        return channel

    async def fake_run(_channel, runtime_request: RuntimeTurnRequest):
        seen.append(runtime_request.turn_id)

    monkeypatch.setenv("VC_BUS_SOCK", "/tmp/fake-runtime-bus.sock")
    monkeypatch.setattr(sandbox_entry, "connect_bus", fake_connect_bus)
    monkeypatch.setattr(sandbox_entry, "_run", fake_run)

    assert await sandbox_entry.main() == 0
    assert seen == ["turn-1", "turn-2"], channel.sent
    assert channel.closed is True


def test_runtime_protocol_import_does_not_eagerly_load_host_orchestrator() -> None:
    code = (
        "import sys; "
        "import vibecanvas_api.services.agent_runtime.protocol; "
        "assert 'vibecanvas_api.services.agent_runtime.orchestrator' not in sys.modules; "
        "assert 'vibecanvas_api.services.agent_runtime.orchestrator' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_runtime_skill_requires_immutable_mounted_revision() -> None:
    skill = RuntimeSkill(
        skill_id="skill-1",
        name="review-data",
        revision_hash="a" * 64,
        root_path="/skills/skill-1/revisions/" + "a" * 64,
    )
    assert skill.root_path.startswith("/skills/")
    with pytest.raises(ValidationError, match="under /skills"):
        RuntimeSkill(
            skill_id="skill-1",
            name="review-data",
            revision_hash="a" * 64,
            root_path="/mount/skills/review-data",
        )


def test_runtime_open_requires_runtime_namespace() -> None:
    with pytest.raises(ValidationError, match="under /runtime"):
        RuntimeOpenRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            runtime_type="codex",
            runtime_session_id="session",
            runtime_root="/data/codex",
        )


def test_turn_settings_are_runtime_neutral_and_per_turn() -> None:
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={
            "id": "gpt-example",
            "base_url": "http://platform.test/api/internal/runtime-model/v1",
            "api_key": "turn-capability",
        },
        reasoning_effort="ultra",
        approval_mode="always_ask",
        active_platform_mcps=["workflow"],
        mcp_host_servers=[{
            "name": "workflow",
            "source": "platform",
            "connection": {
                "transport": "host_gateway",
                "capability": "private",
            },
        }],
    )
    assert request.reasoning_effort == "ultra"
    assert request.active_platform_mcps == ["workflow"]
    assert request.instructions == []


def test_codex_rejects_platform_conversation_clock() -> None:
    with pytest.raises(ValidationError, match="owns conversation time context"):
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="turn",
            runtime_type="codex",
            runtime_session_id="session",
            runtime_root="/runtime/.codex",
            message={"role": "user", "content": "hello"},
            model={
                "id": "gpt-example",
                "base_url": "http://platform.test/api/internal/runtime-model/v1",
                "api_key": "turn-capability",
            },
            conversation_clock={
                "timezone": "Asia/Shanghai",
                "started_at": "2026-08-02T00:00:00Z",
            },
        )


def test_runtime_instructions_are_backend_resolved_and_match_active_modes() -> None:
    common = {
        "tenant_id": "tenant",
        "user_id": "user",
        "chat_id": "chat",
        "turn_id": "turn",
        "runtime_type": "codex",
        "runtime_session_id": "session",
        "runtime_root": "/runtime/.codex",
        "message": {"role": "user", "content": "build a workflow"},
        "model": {"id": "gpt-test", "connection_type": "chatgpt_account"},
        "command_context": {
            "active_modes": ["workflow"],
            "activated_this_turn": ["workflow"],
        },
    }
    with pytest.raises(ValidationError, match="exactly match active_modes"):
        RuntimeTurnRequest(**common)

    request = RuntimeTurnRequest(
        **common,
        instructions=[{
            "instruction_id": "command:build:v1",
            "kind": "command_context",
            "scope": "chat",
            "name": "workflow",
            "version": 1,
            "content": "backend resolved build instructions",
            "activated_this_turn": True,
        }],
    )
    assert request.message["content"] == "build a workflow"
    assert request.instructions[0].content == "backend resolved build instructions"

    mismatched = {
        **common,
        "command_context": {
            "active_modes": ["workflow"],
            "activated_this_turn": [],
        },
    }
    with pytest.raises(ValidationError, match="activated_this_turn"):
        RuntimeTurnRequest(
            **mismatched,
            instructions=[request.instructions[0].model_dump()],
        )


def test_runtime_turn_requires_exact_platform_descriptor_set() -> None:
    common = {
        "tenant_id": "tenant",
        "user_id": "user",
        "chat_id": "chat",
        "turn_id": "turn",
        "runtime_type": "codex",
        "runtime_session_id": "session",
        "runtime_root": "/runtime/.codex",
        "message": {"role": "user", "content": "/workflow"},
        "model": {"id": "gpt-test", "connection_type": "chatgpt_account"},
    }
    with pytest.raises(ValidationError, match="exactly match"):
        RuntimeTurnRequest(**common, active_platform_mcps=["workflow"])
    with pytest.raises(ValidationError, match="exactly match"):
        RuntimeTurnRequest(
            **common,
            mcp_host_servers=[{
                "name": "workflow",
                "source": "platform",
                "connection": {
                    "transport": "host_gateway",
                    "capability": "private",
                },
            }],
        )


def test_runtime_command_context_rejects_backend_domain_objects() -> None:
    with pytest.raises(ValidationError, match="workflow"):
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="turn",
            runtime_type="codex",
            runtime_session_id="session",
            runtime_root="/runtime/.codex",
            message={"role": "user", "content": "/workflow"},
            model={"id": "gpt-test", "connection_type": "chatgpt_account"},
            command_context={
                "workspace_scope_id": "workspace",
                "active_modes": ["workflow"],
                "workflow": {"node_1": {"node_type": "StartNode"}},
            },
        )


def test_runtime_event_rejects_unversioned_or_unknown_wire_types() -> None:
    event = RuntimeEvent(
        event_id="evt-1",
        seq=1,
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="session",
        type="message.delta",
        payload={"text": "a"},
    )
    assert event.protocol_version == 2
    with pytest.raises(ValidationError):
        RuntimeEvent(
            event_id="evt-2",
            seq=2,
            chat_id="chat",
            turn_id="turn",
            runtime_type="codex",
            runtime_session_id="session",
            type="unknown.runtime.event",
        )


def test_runtime_control_separates_platform_id_from_codex_correlation() -> None:
    response = RuntimeControlResponse(
        request_id="hitl_123",
        chat_id="chat",
        turn_id="turn",
        gate_type="pre_tool_approval",
        action="approve",
        correlation={
            "source": "codex_app_server",
            "runtime_request_id": 61,
            "runtime_method": "item/commandExecution/requestApproval",
            "runtime_thread_id": "thr_123",
            "runtime_turn_id": "turn_123",
            "runtime_item_id": "call_123",
        },
    )
    assert response.request_id == "hitl_123"
    assert response.correlation.runtime_request_id == 61


def test_runtime_control_rejects_approval_action_for_post_tool_gate() -> None:
    with pytest.raises(ValidationError, match="invalid for gate"):
        RuntimeControlResponse(
            request_id="hitl_123",
            chat_id="chat",
            turn_id="turn",
            gate_type="post_tool_interaction",
            action="approve",
            correlation={
                "source": "platform_mcp",
                "runtime_request_id": "rpc-7",
                "runtime_method": "mcpServer/elicitation/request",
            },
        )
