from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from vibecanvas_api.routes.chats import (
    _hitl_history_projection,
    _merge_hitl_history_projections,
)
from vibecanvas_api.schemas.chat import HistoryMessage


@pytest.mark.parametrize("call_id", ["cli_123", "transfer_123"])
def test_cli_approval_restores_without_native_mcp_tool_call(call_id):
    projection = HistoryMessage(id="hitl:cli:projection", role="tool", content="Waiting",
                                tool_call_id=call_id, ts=123, artifact={"pending": True})
    result = _merge_hitl_history_projections([], [(call_id, projection)])
    assert len(result) == 2
    assert result[0].tool_calls[0]["id"] == call_id
    assert result[1] is projection
    assert _merge_hitl_history_projections(result, [(call_id, projection)]) == result


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
    projected = _hitl_history_projection(
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
    projected = _hitl_history_projection(artifact, hitl)
    messages = _merge_hitl_history_projections([], [projected])
    assert messages[0].tool_calls[0] == {"id": "cli_123", "name": "flowork-cli " + method.replace(".", " "),
                                        "args": {"deployment_id": "target"}}


def test_hitl_history_projection_freezes_resolved_result():
    result = {"decision": "approve", "remember": False}
    projected = _hitl_history_projection(
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
    _, message = _hitl_history_projection(
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
    _, projection = _hitl_history_projection(
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

    merged = _merge_hitl_history_projections(
        history,
        [("call_1", projection)],
    )

    assert len(merged) == 3
    assert merged[1].content == "command output"
    assert merged[1].artifact == projection.artifact
    assert merged[1].artifact["payload"]["pending_approval"] is False


def test_pending_card_is_inserted_after_announcing_tool_call():
    _, projection = _hitl_history_projection(
        *_rows(status="pending", interacted=False, result={})
    )
    history = [
        HistoryMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "shell", "arguments": "{}"}],
        ),
    ]

    merged = _merge_hitl_history_projections(
        history,
        [("call_1", projection)],
    )

    assert [message.role for message in merged] == ["assistant", "tool"]
    assert merged[1].id == "hitl:hitl_1:projection"
