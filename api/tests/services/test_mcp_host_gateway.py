from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.agent_runtime import mcp_host_gateway
from vibecanvas_api.services.agent_runtime.protocol import RuntimeEvent, RuntimeTurnRequest


@pytest.mark.asyncio
async def test_retired_browser_launch_never_returns_authority(monkeypatch):
    monkeypatch.setattr(mcp_host_gateway, "_verify_private_execution", lambda *_args: None)

    def no_credential_access(*_args):
        raise AssertionError("Retired launch must not read browser credentials")

    monkeypatch.setattr(mcp_host_gateway, "_platform_capability_token", no_credential_access)
    request = RuntimeTurnRequest(
        tenant_id="tenant", user_id="user", chat_id="chat", turn_id="turn",
        runtime_type="codex", runtime_session_id="runtime", runtime_root="/runtime/.codex",
        model={"id": "test", "connection_type": "chatgpt_account"},
        message={"role": "user", "content": "inspect"},
        active_platform_mcps=["interactive", "browser"],
        mcp_host_servers=[{
            "name": name, "source": "platform", "server_id": f"platform:{name}",
            "config_revision": "v1", "connection": {"transport": "host_gateway", "capability": "private"},
        } for name in ("interactive", "browser")],
    )
    event = RuntimeEvent(
        event_id="evt", seq=1, chat_id="chat", turn_id="turn", runtime_type="codex",
        runtime_session_id="runtime", type="mcp.gateway.requested",
        payload={
            "request_id": "old-launch", "operation": "launch", "server": "browser",
            "execution_capability": "private",
            "runtime_correlation": {
                "source": "mcp_hub", "runtime_request_id": "old-launch", "runtime_method": "launch",
            },
        },
    )
    response = await mcp_host_gateway.handle_mcp_gateway_request(event, request)
    assert response.action == "rejected"
    assert "retired" in response.error
    assert response.payload == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,valid", [("config.get", True), ("workflow.list", True), ("workflow.create", True), ("workflow.connect", False), ("workflow.update", True), ("workflow.get", True), ("workflow.download", True), ("workflow.upload", True), ("workflow.check", True), ("workflow.version.list", True), ("workflow.version.create", True), ("workflow.version.set", False), ("workflow.delete", True), ("cli.start", True), ("cli.poll", True), ("cli.cancel", True)])
async def test_cli_private_dispatch_is_allowlisted_and_needs_no_workflow_mcp(monkeypatch, operation, valid):
    from vibecanvas_api.services.agent_runtime import cli_host

    checked = []
    monkeypatch.setattr(mcp_host_gateway, "_verify_private_execution", lambda call, request: checked.append(call.operation))
    invoke = AsyncMock(return_value={"workflows": [], "next_offset": None})
    monkeypatch.setattr(cli_host, "invoke_workflow_command", invoke)
    request = RuntimeTurnRequest(
        tenant_id="tenant", user_id="user", chat_id="chat", turn_id="turn",
        runtime_type="codex", runtime_session_id="runtime", runtime_root="/runtime/.codex",
        model={"id": "test", "connection_type": "chatgpt_account"}, message={"role": "user", "content": "list"},
        active_platform_mcps=["cli"],
        mcp_host_servers=[{
            "name": "cli", "source": "platform", "server_id": "platform:cli",
            "config_revision": "rev-1", "connection": {"transport": "host_gateway", "capability": "host-only"},
        }],
    )
    event = RuntimeEvent(
        event_id="evt", seq=1, chat_id="chat", turn_id="turn", runtime_type="codex",
        runtime_session_id="runtime", type="mcp.gateway.requested",
        payload={
            "request_id": "cli-1", "operation": "cli_call", "server": "cli",
            "tool_name": operation, "arguments": {"limit": 5},
            "execution_capability": "private-execution-token",
            "runtime_correlation": {"source": "mcp_hub", "runtime_request_id": "cli-1", "runtime_method": "cli_call"},
        },
    )
    response = await mcp_host_gateway.handle_mcp_gateway_request(event, request)
    assert checked == ["cli_call"]
    if valid:
        assert response.action == "accepted"
        assert response.payload == {"workflows": [], "next_offset": None}
        invoke.assert_awaited_once_with(operation=operation, identity_token="host-only", arguments={"limit": 5})
    else:
        assert response.action == "rejected"
        invoke.assert_not_awaited()

    def reject(*_args):
        raise PermissionError("invalid or expired MCP execution capability")

    invoke.reset_mock()
    monkeypatch.setattr(mcp_host_gateway, "_verify_private_execution", reject)
    response = await mcp_host_gateway.handle_mcp_gateway_request(event, request)
    assert response.action == "rejected"
    invoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("active,server", [("interactive", "cli"), ("cli", "workflow"), ("cli", "interactive")])
async def test_cli_gateway_cannot_borrow_another_surface_or_inactive_authority(monkeypatch, active, server):
    from vibecanvas_api.services.agent_runtime import cli_host

    monkeypatch.setattr(mcp_host_gateway, "_verify_private_execution", lambda *_: None)
    invoke = AsyncMock()
    monkeypatch.setattr(cli_host, "invoke_workflow_command", invoke)
    request = RuntimeTurnRequest(
        tenant_id="tenant", user_id="user", chat_id="chat", turn_id="turn",
        runtime_type="codex", runtime_session_id="runtime", runtime_root="/runtime/.codex",
        model={"id": "test", "connection_type": "chatgpt_account"},
        message={"role": "user", "content": "list"}, active_platform_mcps=[active],
        mcp_host_servers=[{"name": active, "source": "platform",
                           "connection": {"transport": "host_gateway", "capability": "private"}}],
    )
    event = RuntimeEvent(
        event_id="evt", seq=1, chat_id="chat", turn_id="turn", runtime_type="codex",
        runtime_session_id="runtime", type="mcp.gateway.requested",
        payload={"request_id": "cli-1", "operation": "cli_call", "server": server,
                 "tool_name": "workflow.list", "arguments": {}, "execution_capability": "signed",
                 "runtime_correlation": {"source": "mcp_hub", "runtime_request_id": "cli-1", "runtime_method": "cli_call"}},
    )
    assert (await mcp_host_gateway.handle_mcp_gateway_request(event, request)).action == "rejected"
    invoke.assert_not_awaited()


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
