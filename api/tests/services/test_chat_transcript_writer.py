from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from vibecanvas_api.services.chat_transcript_writer import ChatTranscriptWriter


@pytest.mark.asyncio
async def test_tool_carrier_is_persisted_before_its_result(monkeypatch):
    writes: list[dict] = []

    class FakeRepo:
        def __init__(self, _session, _user_id):
            pass

        async def persist_message(self, _chat_id: str, message: dict) -> None:
            writes.append(message)

        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def fake_session_scope(**_kwargs):
        yield object()

    monkeypatch.setattr(
        "vibecanvas_api.services.chat_transcript_writer.ChatRepo",
        FakeRepo,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.chat_transcript_writer.session_scope",
        fake_session_scope,
    )

    writer = ChatTranscriptWriter(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
    )
    await writer.consume(
        "CHAT_EVENT",
        {
            "type": "message_start",
            "message_id": "carrier",
            "role": "assistant",
        },
    )
    await writer.consume(
        "CHAT_EVENT",
        {
            "type": "tool_start",
            "message_id": "carrier",
            "tool_call_id": "call_1",
            "name": "render_interactive",
            "arguments": "{}",
            "invocation": {
                "schemaVersion": 1,
                "invocationId": "call_1",
                "runtime": {"type": "codex"},
                "status": "running",
            },
        },
    )
    await writer.consume(
        "CHAT_EVENT",
        {
            "type": "tool_end",
            "tool_call_id": "call_1",
            "name": "render_interactive",
            "status": "done",
            "content": "rendered",
            "artifact": {"status": "success"},
            "invocation": {
                "schemaVersion": 1,
                "invocationId": "call_1",
                "runtime": {"type": "codex"},
                "status": "success",
            },
        },
    )
    await writer.close()

    assert [message["role"] for message in writes] == ["assistant", "tool"]
    assert writes[0]["message_id"] == "carrier"
    assert writes[0]["meta"]["status"] == "completed"
    assert writes[0]["content"]["tool_calls"][0]["id"] == "call_1"
    assert writes[0]["content"]["tool_calls"][0]["invocation"]["status"] == "running"
    assert writes[1]["content"]["tool_call_id"] == "call_1"
    assert writes[1]["content"]["artifact"] == {"status": "success"}
    assert writes[1]["content"]["invocation"]["status"] == "success"
