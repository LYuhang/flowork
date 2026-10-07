from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from vibecanvas_api.services.chat_history_projection import (
    hitl_history_projection,
    merge_hitl_history_projections,
)
from vibecanvas_api.schemas.chat import HistoryMessage


@pytest.mark.parametrize("call_id", ["cli_123", "transfer_123"])
def test_cli_approval_restores_without_native_mcp_tool_call(call_id):
    projection = HistoryMessage(id="hitl:cli:projection", role="tool", content="Waiting",
                                tool_call_id=call_id, ts=123, artifact={"pending": True})
    result = merge_hitl_history_projections([], [(call_id, projection)])
    assert len(result) == 2
    assert result[0].tool_calls[0]["id"] == call_id
    assert result[1] is projection
    assert merge_hitl_history_projections(result, [(call_id, projection)]) == result


def _rows(
    *,
    status: str,
    interacted: bool,
    result: dict,
    hitl_type: str = "pre_tool_approval",
):
    hitl = SimpleNamespace(
        hitl_request_id="hitl_1",
        hitl_type=hitl_type,
        status=status,
        ui_payload_json={
            "projection_event": {
                "type": "tool_update",
                "tool_call_id": "call_1",
                "status": "running",
                "artifact": {
                    "status": "success",
                    "content": "Waiting for user approval.",
                    "payload": {
                        "pending_approval": True,
                        "artifact": {"interaction_state": {"status": "pending"}},
                    },
                    "meta": {"pending_approval": True},
                },
            }
        },
    )
    artifact = SimpleNamespace(
        artifact_id="artifact_1",
        chat_id="chat_1",
        run_id="run_1",
        hitl_request_id="hitl_1",
        title="Authorize execution",
        component_type="approval",
        completion_mode="wait_for_submit",
        definition_json={
            "kind": "interactive_artifact",
            "props": {"fields": [{"name": "tool", "value": "shell"}]},
        },
        widget_state_json={},
        interaction_result_json=result,
        is_interacted=interacted,
        artifact_ref=None,
        content_hash=None,
        created_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    return artifact, hitl


def test_hitl_history_projection_preserves_pending_card():
    projected = hitl_history_projection(
        *_rows(status="pending", interacted=False, result={})
    )

    assert projected is not None
    tool_call_id, message = projected
    assert tool_call_id == "call_1"
    assert message.tool_call_id == "call_1"
    assert message.artifact["payload"]["pending_approval"] is True
    state = message.artifact["payload"]["artifact"]["interaction_state"]
    assert state == {"is_interacted": False, "status": "pending", "result": {}}


@pytest.mark.parametrize("method", ["workflow.delete", "task.create", "deployment.delete", "deployment.update"])
def test_cli_history_restores_actual_command_and_arguments(method):
    artifact, hitl = _rows(status="approved", interacted=True, result={"decision": "approve"})
    hitl.ui_payload_json["projection_event"]["tool_call_id"] = "cli_123"
    hitl.runtime_correlation_json = {"source": "flowork_cli", "runtime_method": method}
    hitl.agent_payload_json = {"arguments": {"deployment_id": "target"}}
    projected = hitl_history_projection(artifact, hitl)
    messages = merge_hitl_history_projections([], [projected])
    assert messages[0].tool_calls[0] == {"id": "cli_123", "name": "flowork-cli " + method.replace(".", " "),
                                        "args": {"deployment_id": "target"}}


def test_hitl_history_projection_freezes_resolved_result():
    result = {"decision": "approve", "remember": False}
    projected = hitl_history_projection(
        *_rows(status="approved", interacted=True, result=result)
    )

    assert projected is not None
    _, message = projected
    assert message.id == "hitl:hitl_1:projection"
    assert message.artifact["payload"]["pending_approval"] is False
    assert message.artifact["meta"]["pending_approval"] is False
    state = message.artifact["payload"]["artifact"]["interaction_state"]
    assert state == {
        "is_interacted": True,
        "status": "approved",
        "result": result,
    }


def test_post_tool_continue_projection_does_not_become_tool_approval():
    _, message = hitl_history_projection(
        *_rows(
            status="pending",
            interacted=False,
            result={},
            hitl_type="post_tool_review",
        )
    )

    assert message.artifact["payload"]["hitl_type"] == "post_tool_review"
    assert "pending_approval" not in message.artifact["payload"]
    assert "pending_approval" not in message.artifact["meta"]
    assert (
        message.artifact["payload"]["artifact"]["component_type"]
        == "approval"
    )


def test_completed_tool_result_keeps_content_and_gains_frozen_card():
    _, projection = hitl_history_projection(
        *_rows(status="approved", interacted=True, result={"decision": "approve"})
    )
    history = [
        HistoryMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "shell", "arguments": "{}"}],
        ),
        HistoryMessage(
            role="tool",
            content="command output",
            tool_call_id="call_1",
        ),
        HistoryMessage(role="assistant", content="done"),
    ]

    merged = merge_hitl_history_projections(
        history,
        [("call_1", projection)],
    )

    assert len(merged) == 3
    assert merged[1].content == "command output"
    assert merged[1].artifact == projection.artifact
    assert merged[1].artifact["payload"]["pending_approval"] is False


def test_pending_card_is_inserted_after_announcing_tool_call():
    _, projection = hitl_history_projection(
        *_rows(status="pending", interacted=False, result={})
    )
    history = [
        HistoryMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "shell", "arguments": "{}"}],
        ),
    ]

    merged = merge_hitl_history_projections(
        history,
        [("call_1", projection)],
    )

    assert [message.role for message in merged] == ["assistant", "tool"]
    assert merged[1].id == "hitl:hitl_1:projection"


@pytest.mark.asyncio
async def test_history_pagination_counts_durable_rows_not_projection_cards(monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.requests import Request
    from vibecanvas_api.routes import chats
    from vibecanvas_api.schemas.pagination import PageRequest

    monkeypatch.setattr(chats, '_authorize_chat', AsyncMock())
    repo = SimpleNamespace(
        get_authorized_inventory=AsyncMock(return_value={'scope_id': 'scope'}),
        list_message_page=AsyncMock(return_value=([
            {'message_id': 'hidden', 'content': {'visibility': 'hidden'}},
            {'message_id': 'visible', 'role': 'assistant', 'content': {'text': 'Hello'}, 'ts': 12},
        ], 32, 30)),
    )
    hitl = SimpleNamespace(list_artifact_refs_for_chat=AsyncMock(return_value=[(None, None)]))
    projection = HistoryMessage(id='approval', role='tool', content='Waiting', ts=13,
                                tool_call_id='cli_123')
    monkeypatch.setattr(chats, 'hitl_history_projection', lambda *args: ('cli_123', projection))
    result = await chats.get_chat_history(
        scope_id='scope', chat_id='chat', request=Request({'type': 'http'}),
        page=PageRequest(limit=30), chat_repo=repo, hitl_repo=hitl,
        auth=SimpleNamespace(user_id="00000000-0000-0000-0000-000000000001"), debug=False, tail=True, before_turn_id=None, service=None,
    )
    assert result.total == 32
    assert result.offset == 30
    assert len(result.items) == 3
    assert result.items[0].history_position == 31
    assert result.items[-1].history_position is None

@pytest.mark.asyncio
async def test_history_attaches_failure_only_to_its_user_message(monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.requests import Request
    from vibecanvas_api.routes import chats
    from vibecanvas_api.schemas.pagination import PageRequest
    monkeypatch.setattr(chats, '_authorize_chat', AsyncMock())
    failed = AsyncMock(return_value={'failed_turn': {'code': 'engine_error', 'message': 'authorization_unavailable'}})
    monkeypatch.setattr(chats, 'AgentRunsRepo', lambda session: SimpleNamespace(failed_turns_for_history=failed))
    repo = SimpleNamespace(
        get_authorized_inventory=AsyncMock(return_value={'scope_id': 'scope'}),
        list_message_page=AsyncMock(return_value=([
            {'message_id': 'user-1', 'role': 'user', 'turn_id': 'failed_turn', 'content': {'text': 'First request'}},
            {'message_id': 'user-2', 'role': 'user', 'turn_id': 'next_turn', 'content': {'text': 'Next request'}},
            {'message_id': 'reply', 'role': 'assistant', 'turn_id': 'next_turn', 'content': {'text': 'Hello'}},
        ], 3, 0)),
    )
    result = await chats.get_chat_history(
        scope_id='scope', chat_id='chat', request=Request({'type': 'http'}),
        page=PageRequest(limit=30), chat_repo=repo,
        hitl_repo=SimpleNamespace(list_artifact_refs_for_chat=AsyncMock(return_value=[])),
        auth=SimpleNamespace(user_id='actor'), debug=False, tail=True,
        before_turn_id=None, service=None, session=None,
    )
    assert result.items[0].meta == {'turn_error': {'code': 'engine_error', 'message': 'authorization_unavailable'}}
    assert result.items[1].meta is None
    assert result.items[2].meta is None
    assert failed.call_args.kwargs == {'creator_user_id': 'actor'}
    assert set(failed.call_args.args[1]) == {'failed_turn', 'next_turn'}
