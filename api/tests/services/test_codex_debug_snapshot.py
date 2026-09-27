from __future__ import annotations

import json

from vibecanvas_api.services.agent_runtime import codex_debug_snapshot as snapshot_module
from vibecanvas_api.services.agent_runtime.codex_debug_snapshot import (
    build_codex_debug_snapshot,
    capture_codex_command_output_observations,
    write_codex_debug_snapshot,
)
from vibecanvas_api.services.agent_runtime.protocol import RuntimeTurnRequest


def _request(*, durable_history: dict | None = None) -> RuntimeTurnRequest:
    kwargs: dict = {}
    if durable_history is not None:
        kwargs["durable_history"] = durable_history
    return RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat/unsafe",
        turn_id="turn/2",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        runtime_state_ref="codex-thread",
        message={"role": "user", "content": "current question"},
        model={
            "id": "gpt-codex-current",
            "base_url": "http://platform.test/api/internal/runtime-model/v1",
            "api_key": "turn-capability",
        },
        reasoning_effort="high",
        **kwargs,
    )


def test_codex_snapshot_projects_native_thread_and_current_turn_input() -> None:
    request = _request()
    thread = {
        "id": "codex-thread",
        "modelProvider": "openai",
        "turns": [
            {
                "id": "native-turn-1",
                "itemsView": "full",
                "items": [
                    {
                        "id": "user-1",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "earlier question"}],
                    },
                    {
                        "id": "reasoning-1",
                        "type": "reasoning",
                        "summary": ["Checked the relevant files."],
                        "content": ["private hidden reasoning"],
                    },
                    {
                        "id": "command-1",
                        "type": "commandExecution",
                        "command": "rg foo",
                        "cwd": "/data",
                        "status": "completed",
                        "aggregatedOutput": "match",
                    },
                    {
                        "id": "assistant-1",
                        "type": "agentMessage",
                        "text": "Earlier answer",
                        "phase": "final_answer",
                    },
                ],
            }
        ],
    }
    current_input = [{
        "type": "text",
        "text": (
            "<resolved-command-context>backend context</resolved-command-context>\n"
            "<user-message>current question</user-message>"
        ),
    }]

    snapshot = build_codex_debug_snapshot(
        request=request,
        thread=thread,
        thread_id="codex-thread",
        current_input=current_input,
    )

    assert snapshot["runtime_type"] == "codex"
    assert snapshot["snapshot_semantics"] == "runtime_thread_input"
    assert snapshot["target"]["provider"] == "openai"
    assert snapshot["target"]["model_id"] == "gpt-codex-current"
    assert snapshot["token_total"] is None
    assert snapshot["runtime_metadata"]["history_complete"] is True
    assert [item["runtime_item_type"] for item in snapshot["messages"]] == [
        "userMessage",
        "reasoning",
        "commandExecution",
        "agentMessage",
        "turnInput",
    ]
    assert snapshot["messages"][1]["content"] == "Checked the relevant files."
    assert snapshot["messages"][1]["runtime_metadata"]["has_hidden_content"] is True
    assert "private hidden reasoning" not in json.dumps(snapshot)
    assert "backend context" in snapshot["messages"][-1]["content"]
    assert snapshot["messages"][-1]["runtime_metadata"]["current_turn"] is True
    assert "/" not in snapshot["snapshot_id"]


def test_codex_snapshot_marks_summarized_native_history() -> None:
    snapshot = build_codex_debug_snapshot(
        request=_request(),
        thread={
            "id": "codex-thread",
            "turns": [{
                "id": "native-turn-1",
                "itemsView": "summary",
                "items": [],
            }],
        },
        thread_id="codex-thread",
        current_input=[{"type": "text", "text": "hello"}],
    )

    assert snapshot["runtime_metadata"]["history_complete"] is False


def test_large_snapshot_keeps_every_message_and_can_restore_full_content(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setattr(snapshot_module, "DEBUG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DEBUG_VIEW_ENABLED", "1")
    content = "长消息🙂" * 90_000
    thread = {"turns": [{"id": f"turn-{index}", "items": [
        {"type": "agentMessage", "id": f"message-{index}", "text": content + str(index)},
    ]} for index in range(12)]}
    payload = build_codex_debug_snapshot(request=_request(), thread=thread, thread_id="native",
        current_input=[{"type": "text", "text": "Current question is still here."}])
    path = write_codex_debug_snapshot(payload)
    saved = json.loads(Path(path).read_text())
    assert len(saved["messages"]) == 13
    assert saved["messages"][-1]["content"] == "Current question is still here."
    assert saved["runtime_metadata"]["snapshot_truncated"] is False
    assert Path(path).stat().st_size < 100_000
    for index, message in enumerate(saved["messages"][:-1]):
        assert "_full_content" not in message
        assert len(message["content"]) == 1200
        restored = "".join((Path(message["content_ref"]) / f"{part}.txt").read_text()
                           for part in range(message["content_part_count"]))
        assert restored == content + str(index)


def test_recovery_envelope_projects_individual_roles_turns_and_no_duplicate_history():
    from vibecanvas_api.services.agent_runtime.codex import _turn_input
    request = _request(durable_history={"messages": [
        {"message_id": "one", "turn_id": "first", "role": "user", "text": "Question one"},
        {"message_id": "two", "turn_id": "first", "role": "assistant", "text": "Answer one"},
    ]})
    current = _turn_input(request, recovered_native_history=True)
    snapshot = build_codex_debug_snapshot(request=request, thread={}, thread_id="native", current_input=current)
    assert len(snapshot["messages"]) == 3
    assert [item["role"] for item in snapshot["messages"]] == ["user", "assistant", "user"]
    assert snapshot["messages"][0]["runtime_metadata"]["codex_turn_id"] == "first"
    assert "<durable-conversation-history>" not in snapshot["messages"][-1]["content"]
    assert "current question" in snapshot["messages"][-1]["content"]
    native = build_codex_debug_snapshot(request=_request(), thread={"turns": [{"id": "recovered", "items": [
        {"type": "userMessage", "id": "envelope", "content": current},
    ]}]}, thread_id="native", current_input=[{"type": "text", "text": "Next question"}])
    assert [item["content"] for item in native["messages"][:2]] == ["Question one", "Answer one"]
    assert native["messages"][0]["runtime_item_type"] == "recoveredHistoryMessage"


def test_recovery_projection_accepts_null_tool_calls_without_merging_messages():
    records = [{"role": "user", "text": "Question", "tool_calls": None},
               {"role": "assistant", "text": "Answer", "tool_calls": []}]
    content = ("<system-reminder>\nThe native Codex thread did not prove that it covered the durable product transcript.\n"
               "<durable-conversation-history>" + json.dumps(records) +
               "</durable-conversation-history></system-reminder>Current")
    message = {"content": content, "role": "user"}
    projected = snapshot_module._expand_recovery_message(message)
    assert [item["content"] for item in projected[:2]] == ["Question", "Answer"]
    records[0]["tool_calls"] = "not an array"
    invalid = {**message, "content": content.replace('"tool_calls": null', '"tool_calls": "not an array"')}
    assert snapshot_module._expand_recovery_message(invalid) == [invalid]


def test_codex_snapshot_falls_back_to_durable_history_when_native_thread_is_empty() -> None:
    """``thread/start``/``thread/fork`` never return a transcript, so a fresh

    fork (a routine event on an ordinary config change) leaves ``thread``
    with no ``turns`` even though the product conversation has several. The
    Inspector must still show that conversation from the durable transcript.
    """
    request = _request(durable_history={
        "messages": [
            {
                "message_id": "m1",
                "turn_id": "turn/0",
                "role": "user",
                "text": "earlier question",
            },
            {
                "message_id": "m2",
                "turn_id": "turn/0",
                "role": "assistant",
                "text": "earlier answer",
            },
        ],
        "last_turn_id": "turn/0",
    })

    snapshot = build_codex_debug_snapshot(
        request=request,
        thread={"id": "codex-thread-refreshed"},
        thread_id="codex-thread-refreshed",
        current_input=[{"type": "text", "text": "current question"}],
    )

    assert snapshot["runtime_metadata"]["history_source"] == "durable_history_fallback"
    assert snapshot["runtime_metadata"]["history_complete"] is True
    assert snapshot["runtime_metadata"]["prior_turn_count"] == 1
    assert [item["runtime_item_type"] for item in snapshot["messages"]] == [
        "durableHistoryMessage",
        "durableHistoryMessage",
        "turnInput",
    ]
    assert snapshot["messages"][0]["content"] == "earlier question"
    assert snapshot["messages"][1]["content"] == "earlier answer"


def test_codex_snapshot_write_is_atomic_and_debug_gated(
    monkeypatch, tmp_path
) -> None:
    debug_dir = tmp_path / "logs" / ".debug"
    monkeypatch.setattr(snapshot_module, "DEBUG_DIR", str(debug_dir))
    payload = build_codex_debug_snapshot(
        request=_request(),
        thread={"id": "codex-thread", "turns": []},
        thread_id="codex-thread",
        current_input=[{"type": "text", "text": "hello"}],
    )

    monkeypatch.delenv("AGENT_DEBUG_VIEW_ENABLED", raising=False)
    assert write_codex_debug_snapshot(payload) is None
    assert not debug_dir.exists()

    monkeypatch.setenv("AGENT_DEBUG_VIEW_ENABLED", "1")
    path = write_codex_debug_snapshot(payload)

    assert path is not None
    assert json.loads(debug_dir.joinpath(path.rsplit("/", 1)[-1]).read_text())[
        "runtime_type"
    ] == "codex"
    assert list(debug_dir.glob("*.tmp")) == []


def test_native_output_observation_file_is_private_atomic_and_outside_snapshot_index(monkeypatch, tmp_path) -> None:
    debug_dir = tmp_path / "logs" / ".debug"
    monkeypatch.setattr(snapshot_module, "DEBUG_DIR", str(debug_dir))
    observations = {"events": [{"item_id": "exec-1", "aggregate_chars": 0}], "dropped_events": 0}
    monkeypatch.delenv("AGENT_DEBUG_VIEW_ENABLED", raising=False)
    assert capture_codex_command_output_observations(request=_request(), observations=observations) is None
    assert not debug_dir.exists()
    monkeypatch.setenv("AGENT_DEBUG_VIEW_ENABLED", "1")
    path = capture_codex_command_output_observations(request=_request(), observations=observations)
    assert path is not None
    files = list((debug_dir / "native-events").glob("*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    payload = json.loads(files[0].read_text())
    assert payload["turn_id"] == "turn/2"
    assert payload["boundary"] == "native_app_server_stdout_before_product_projection"
    assert payload["events"] == observations["events"]
    assert list(debug_dir.glob("*.json")) == []
    assert list(debug_dir.rglob("*.tmp")) == []


def test_native_output_observations_preserve_alternate_only_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_DEBUG_VIEW_ENABLED", "1")
    monkeypatch.setattr(snapshot_module, "DEBUG_DIR", str(tmp_path))
    observations = {
        "events": [], "dropped_events": 0,
        "alternate_channel_counts": {"process/outputDelta": 2},
    }
    assert capture_codex_command_output_observations(
        request=_request(), observations=observations,
    ) is not None
    payload = json.loads(next((tmp_path / "native-events").glob("*.json")).read_text())
    assert payload["alternate_channel_counts"] == {"process/outputDelta": 2}
    assert capture_codex_command_output_observations(
        request=_request(), observations={"events": [], "alternate_channel_counts": {}},
    ) is None
