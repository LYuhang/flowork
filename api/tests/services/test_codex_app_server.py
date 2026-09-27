from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vibecanvas_api.services.agent_runtime.codex_app_server import (
    CodexAppServer,
    CodexAppServerError,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_output", ["", None])
async def test_native_output_trace_records_lengths_without_mutating_or_retaining_content(monkeypatch, completed_output) -> None:
    monkeypatch.setenv("AGENT_DEBUG_VIEW_ENABLED", "1")
    server = CodexAppServer(executable="/codex", env={})
    reader = asyncio.StreamReader()
    server._process = SimpleNamespace(stdout=reader)
    common = {"threadId": "thread-1", "turnId": "turn-1"}
    messages = [
        {"method": "item/started", "params": {**common, "item": {
            "id": "exec-1", "type": "commandExecution", "command": "sensitive-command",
        }}},
        {"method": "item/commandExecution/outputDelta", "params": {
            **common, "itemId": "exec-1", "delta": "synthetic-secret-output",
        }},
        {"method": "item/completed", "params": {**common, "item": {
            "id": "exec-1", "type": "commandExecution", "aggregatedOutput": completed_output, "exitCode": 0,
        }}},
    ]
    reader.feed_data("".join(json.dumps(row) + "\n" for row in messages).encode())
    reader.feed_eof()
    await server._read_loop()
    observed = server.take_command_output_observations()
    assert observed["dropped_events"] == 0
    assert [row["sequence"] for row in observed["events"]] == [1, 2, 3]
    assert observed["events"][1]["delta_chars"] == len("synthetic-secret-output")
    assert observed["events"][2]["aggregate_chars"] == (0 if completed_output == "" else None)
    assert all(row["item_id"] == "exec-1" for row in observed["events"])
    assert "synthetic-secret-output" not in json.dumps(observed)
    assert "sensitive-command" not in json.dumps(observed)
    stream = server.messages()
    assert [await anext(stream) for _ in messages] == messages
    assert server.take_command_output_observations() == {
        "events": [], "dropped_events": 0, "alternate_channel_counts": {},
    }


def test_native_output_trace_counts_alternate_channels_without_retaining_payloads(monkeypatch) -> None:
    server = CodexAppServer(executable="/codex", env={})
    monkeypatch.delenv("AGENT_DEBUG_VIEW_ENABLED", raising=False)
    message = {"method": "process/outputDelta", "params": {"data": "synthetic-secret"}}
    server._observe_command_output(message)
    assert server.take_command_output_observations()["alternate_channel_counts"] == {}
    monkeypatch.setenv("AGENT_DEBUG_VIEW_ENABLED", "1")
    methods = [
        "command/exec/outputDelta", "process/outputDelta", "process/exited",
        "codex/event/exec_command_output_delta", "codex/event/exec_command_end",
    ]
    for method in methods:
        for _ in range(2):
            server._observe_command_output({**message, "method": method})
    # Unrecognized method names must not create unbounded keys or retain text.
    server._observe_command_output({**message, "method": "synthetic-secret"})
    observed = server.take_command_output_observations()
    assert observed["alternate_channel_counts"] == dict.fromkeys(methods, 2)
    assert observed["events"] == []
    assert "synthetic-secret" not in json.dumps(observed)
    assert server.take_command_output_observations()["alternate_channel_counts"] == {}


def test_native_output_trace_is_debug_gated_bounded_and_tolerates_unusual_fields(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_DEBUG_VIEW_ENABLED", raising=False)
    server = CodexAppServer(executable="/codex", env={})
    event = {"method": "item/commandExecution/outputDelta", "params": {
        "itemId": "exec-1", "delta": "x",
    }}
    server._observe_command_output(event)
    assert server.take_command_output_observations()["events"] == []
    monkeypatch.setenv("AGENT_DEBUG_VIEW_ENABLED", "1")
    for _ in range(515):
        server._observe_command_output(event)
    observed = server.take_command_output_observations()
    assert len(observed["events"]) == 512
    assert observed["dropped_events"] == 3
    assert observed["events"][0]["sequence"] == 4
    for invalid in ({"method": []}, {"method": [], "params": {}}, {"method": "item/completed", "params": {"item": []}}):
        server._observe_command_output(invalid)
    server._observe_command_output({"method": "item/commandExecution/outputDelta", "params": {
        "itemId": "invalid\nsecret", "delta": {"secret": "not text"},
    }})
    observed = server.take_command_output_observations()
    assert observed["events"][0]["item_id"] is None
    assert observed["events"][0]["delta_chars"] is None
    assert "secret" not in json.dumps(observed)


@pytest.mark.asyncio
async def test_reader_accepts_resume_records_larger_than_asyncio_default() -> None:
    server = CodexAppServer(
        executable="/codex",
        env={},
        read_limit_bytes=256 * 1024,
    )
    reader = asyncio.StreamReader(limit=server._read_limit_bytes)
    server._process = SimpleNamespace(stdout=reader)
    payload = {
        "method": "thread/resumed",
        "params": {"history": "x" * (96 * 1024)},
    }
    reader.feed_data((json.dumps(payload) + "\n").encode())
    reader.feed_eof()

    await server._read_loop()
    messages = server.messages()
    message = await anext(messages)

    assert message == payload
    with pytest.raises(CodexAppServerError, match="closed its output stream"):
        await anext(messages)


def test_reader_limit_rejects_invalid_environment(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_APP_SERVER_JSONL_LIMIT_BYTES", "not-an-integer")

    with pytest.raises(
        RuntimeError,
        match="CODEX_APP_SERVER_JSONL_LIMIT_BYTES must be an integer",
    ):
        CodexAppServer(executable="/codex", env={})


@pytest.mark.asyncio
async def test_server_request_error_response_is_bounded() -> None:
    server = CodexAppServer(executable="/codex", env={})
    server._send = AsyncMock()

    await server.respond_error(
        91,
        code=-32601,
        message="x" * 600,
    )

    server._send.assert_awaited_once_with({
        "id": 91,
        "error": {"code": -32601, "message": "x" * 500},
    })


@pytest.mark.asyncio
async def test_outer_sandboxed_server_disables_redundant_codex_sandbox(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

    async def create_subprocess(*arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            stdin=FakeStdin(),
            stdout=object(),
            returncode=None,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    server = CodexAppServer(
        executable="/codex",
        env={},
        outer_sandboxed=True,
        config_overrides=('model_catalog_json="/tmp/catalog.json"',),
    )
    server._read_loop = AsyncMock()
    server.request = AsyncMock(return_value={})
    server.notify = AsyncMock()

    await server.start()

    assert captured["arguments"] == (
        "/codex",
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        'sandbox_mode="danger-full-access"',
        "-c",
        'model_catalog_json="/tmp/catalog.json"',
        "app-server",
        "--stdio",
    )
    server.request.assert_awaited_once()
    server.notify.assert_awaited_once_with("initialized", {})
