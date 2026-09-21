from __future__ import annotations

from typing import Any

import pytest

from vibecanvas_api.services.agent_runtime import mcp_host_gateway
from vibecanvas_api.services.agent_runtime.protocol import RuntimeTurnRequest


@pytest.mark.asyncio
async def test_remote_gateway_preserves_session_without_exposing_broker_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        status_code = 200
        headers = {
            "content-type": "text/event-stream",
            "mcp-session-id": "session-next",
        }
        content = (
            b"event: message\n"
            b"data: {\"jsonrpc\":\"2.0\",\"id\":7,"
            b"\"result\":{\"tools\":[]}}\n\n"
        )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, **kwargs):
            captured.update({
                "method": method,
                "url": url,
                **kwargs,
            })
            return Response()

    monkeypatch.setattr(
        mcp_host_gateway.httpx,
        "AsyncClient",
        lambda **_kwargs: Client(),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime",
        runtime_root="/runtime/.codex",
        model={"id": "codex:account:gpt-test", "connection_type": "chatgpt_account"},
        message={"role": "user", "content": "hello"},
        mcp_host_servers=[{
            "name": "remote_tools",
            "source": "custom",
            "server_id": "remote-1",
            "config_revision": "rev-1",
            "connection": {
                "transport": "streamable_http",
                "url": "http://api.internal/runtime-mcp/remote-1",
                "headers": {"Authorization": "Bearer host-only-token"},
            },
        }],
    )

    result = await mcp_host_gateway._proxy_remote_message(
        request,
        server="remote_tools",
        arguments={
            "session_id": "session-current",
            "protocol_version": "2025-06-18",
            "message": {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/list",
            },
        },
        close=False,
    )

    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer host-only-token"
    assert captured["headers"]["MCP-Session-Id"] == "session-current"
    assert captured["json"]["method"] == "tools/list"
    assert result == {
        "session_id": "session-next",
        "messages": [{
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"tools": []},
        }],
    }
