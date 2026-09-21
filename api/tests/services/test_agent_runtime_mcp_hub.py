from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.mcp_hub import (
    McpHubInactiveError,
    McpHubReconcileError,
    SandboxMcpHub,
)
from vibecanvas_api.services.agent_runtime.codex_mcp_hub_gateway import (
    CodexMcpHubGateway,
)
from vibecanvas_api.services.agent_runtime.mcp_hub_adapter import (
    SandboxMcpRuntimeAdapter,
)
from vibecanvas_api.services.agent_runtime.mcp_execution_capability import (
    mint_mcp_execution_capability,
    verify_mcp_execution_capability,
)
from vibecanvas_api.services.agent_runtime.mcp_desired_state import (
    build_mcp_lifecycle_contracts,
)
from vibecanvas_api.services.agent_runtime.mcp_runtime_protocol import (
    McpDesiredServer,
    McpDesiredState,
    McpExecutionContext,
)
from vibecanvas_api.services.agent_runtime.protocol import RuntimeTurnRequest
from vibecanvas_api.services.sandbox.manager import SandboxSession


def _server(
    server_id: str,
    *,
    revision: str = "rev-1",
    required: bool = False,
) -> McpDesiredServer:
    if server_id.startswith("platform:"):
        name = server_id.split(":", 1)[1]
        return McpDesiredServer.model_validate({
            "id": server_id,
            "source": "platform",
            "name": name,
            "configurationRevision": revision,
            "required": True,
            "activation": "base",
            "connection": {
                "kind": "platform_facade",
                "capability": name,
            },
        })
    return McpDesiredServer.model_validate({
        "id": server_id,
        "source": "custom_stdio",
        "name": server_id.replace("-", "_"),
        "configurationRevision": revision,
        "required": required,
        "activation": "selected",
        "connection": {
            "kind": "stdio",
            "command": "example-mcp",
            "args": [],
            "cwd": "/data",
        },
    })


def _desired(
    *servers: McpDesiredServer,
    revision: int = 1,
) -> McpDesiredState:
    return McpDesiredState(
        organization_id="tenant",
        user_id="user",
        chat_id="chat",
        runtime_session_id="runtime",
        sandbox_id="sandbox",
        sandbox_generation=7,
        chat_mcp_config_revision=revision,
        platform_contract_revision="platform-contract-1",
        skill_catalog_revision="skill-catalog-1",
        servers=list(servers),
    )


def _context(
    *,
    revision: int = 1,
    turn_id: str = "turn",
    platform_capabilities: list[str] | None = None,
) -> McpExecutionContext:
    now = datetime.now(timezone.utc)
    return McpExecutionContext(
        organization_id="tenant",
        user_id="user",
        chat_id="chat",
        runtime_session_id="runtime",
        sandbox_generation=7,
        turn_id=turn_id,
        agent_run_id=f"run-{turn_id}",
        active_commands=[],
        active_platform_capabilities=(
            ["config"]
            if platform_capabilities is None
            else platform_capabilities
        ),
        selected_mcp_revision=revision,
        approval_mode="agent",
        surface="main",
        authorization_generation="auth-1",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        capability="turn-secret",
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.stopped: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str, dict[str, Any], str]] = []
        self.fail: set[str] = set()

    async def start(self, server: McpDesiredServer) -> tuple[str, ...]:
        self.started.append((server.id, server.configuration_revision))
        if server.id in self.fail:
            raise RuntimeError("start failed")
        return ("example_tool",)

    async def stop(self, server: McpDesiredServer) -> None:
        self.stopped.append((server.id, server.configuration_revision))

    async def call(
        self,
        server: McpDesiredServer,
        tool_name: str,
        arguments: dict[str, Any],
        execution_context: McpExecutionContext,
    ) -> Any:
        self.calls.append((
            server.id,
            tool_name,
            arguments,
            execution_context.turn_id,
        ))
        return {"ok": True}


def test_desired_state_structurally_rejects_credentials_and_upstream_urls() -> None:
    platform = {
        "id": "platform:config",
        "source": "platform",
        "name": "config",
        "configurationRevision": "rev-1",
        "activation": "base",
        "connection": {
            "kind": "platform_facade",
            "capability": "config",
            "headers": {"Authorization": "Bearer secret"},
        },
    }
    with pytest.raises(ValidationError, match="headers"):
        McpDesiredServer.model_validate(platform)

    remote = {
        "id": "remote-1",
        "source": "custom_remote",
        "name": "remote",
        "configurationRevision": "rev-1",
        "activation": "selected",
        "connection": {
            "kind": "host_broker",
            "transport": "streamable_http",
            "brokerRoute": "runtime-mcp:remote-1",
            "url": "https://upstream.example/mcp",
            "token": "secret",
        },
    }
    with pytest.raises(ValidationError, match="url|token"):
        McpDesiredServer.model_validate(remote)

    stdio = {
        "id": "stdio-1",
        "source": "custom_stdio",
        "name": "stdio",
        "configurationRevision": "rev-1",
        "activation": "selected",
        "connection": {
            "kind": "stdio",
            "command": "stdio-mcp",
            "env": {"API_KEY": "secret"},
        },
    }
    with pytest.raises(ValidationError, match="env"):
        McpDesiredServer.model_validate(stdio)


def test_execution_capability_is_signed_scoped_and_expiring() -> None:
    token = mint_mcp_execution_capability(
        organization_id="tenant",
        user_id="user",
        chat_id="chat",
        runtime_session_id="runtime",
        turn_id="turn",
        sandbox_id="sandbox",
        sandbox_generation=7,
        selected_mcp_revision=3,
        active_platform_capabilities=["config", "workflow"],
        authorization_generation="auth-1",
        secret="test-signing-secret",
        ttl_s=60,
        now=100,
    )

    capability = verify_mcp_execution_capability(
        token,
        secret="test-signing-secret",
        now=120,
    )
    assert capability is not None
    assert capability.sandbox_generation == 7
    assert capability.active_platform_capabilities == ("config", "workflow")
    assert verify_mcp_execution_capability(
        token + "x",
        secret="test-signing-secret",
        now=120,
    ) is None
    assert verify_mcp_execution_capability(
        token,
        secret="test-signing-secret",
        now=160,
    ) is None


@pytest.mark.asyncio
async def test_hub_reuses_unchanged_servers_and_reconciles_one_revision() -> None:
    adapter = FakeAdapter()
    hub = SandboxMcpHub(adapter)
    config = _server("platform:config")
    local = _server("local")

    first = await hub.reconcile(_desired(config, local, revision=1))
    unchanged = await hub.reconcile(_desired(config, local, revision=1))
    changed_local = _server("local", revision="rev-2")
    changed = await hub.reconcile(
        _desired(config, changed_local, revision=2)
    )

    assert first.required_ready is True
    assert unchanged.changed_server_ids == []
    assert adapter.started == [
        ("platform:config", "rev-1"),
        ("local", "rev-1"),
        ("local", "rev-2"),
    ]
    assert adapter.stopped == [("local", "rev-1")]
    assert changed.changed_server_ids == ["local"]


@pytest.mark.asyncio
async def test_hub_requires_active_turn_and_filters_platform_capabilities() -> None:
    adapter = FakeAdapter()
    hub = SandboxMcpHub(adapter)
    await hub.reconcile(_desired(_server("platform:config")))

    with pytest.raises(McpHubInactiveError):
        await hub.call("config", "example_tool", {})

    await hub.activate(_context())
    assert await hub.call("config", "example_tool", {"value": 1}) == {
        "ok": True
    }
    await hub.deactivate()
    with pytest.raises(McpHubInactiveError):
        await hub.call("config", "example_tool", {})
    assert adapter.calls == [
        ("platform:config", "example_tool", {"value": 1}, "turn")
    ]


@pytest.mark.asyncio
async def test_required_server_failure_keeps_previous_registry_active() -> None:
    adapter = FakeAdapter()
    hub = SandboxMcpHub(adapter)
    original = _server("platform:config")
    await hub.reconcile(_desired(original, revision=1))
    adapter.fail.add("platform:config")

    with pytest.raises(McpHubReconcileError) as caught:
        await hub.reconcile(
            _desired(
                _server("platform:config", revision="rev-2"),
                revision=2,
            )
        )

    assert caught.value.result.required_ready is False
    status = await hub.status()
    assert status.config_revision == 1
    assert status.servers[0].configuration_revision == "rev-1"


@pytest.mark.asyncio
async def test_executable_hub_keeps_secretless_stdio_session_across_turns() -> None:
    source = """
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sandbox-hub-stdio-test")

@mcp.tool()
def process_identity(value: int) -> dict[str, int]:
    return {"pid": os.getpid(), "value": value}

mcp.run(transport="stdio")
"""
    server = McpDesiredServer.model_validate({
        "id": "stdio-process",
        "source": "custom_stdio",
        "name": "local_process",
        "configurationRevision": "stdio-rev-1",
        "required": False,
        "activation": "selected",
        "connection": {
            "kind": "stdio",
            "command": sys.executable,
            "args": ["-c", source],
            "cwd": "/tmp",
        },
    })

    async def unused_gateway(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("stdio MCP must not call the Host Gateway")

    adapter = SandboxMcpRuntimeAdapter(unused_gateway)
    hub = SandboxMcpHub(adapter)
    try:
        first_reconcile = await hub.reconcile(_desired(server))
        assert first_reconcile.changed_server_ids == ["stdio-process"]

        await hub.activate(_context(
            turn_id="turn-1",
            platform_capabilities=[],
        ))
        first = await hub.call("local_process", "process_identity", {"value": 1})
        await hub.deactivate()

        second_reconcile = await hub.reconcile(_desired(server))
        assert second_reconcile.changed_server_ids == []
        await hub.activate(_context(
            turn_id="turn-2",
            platform_capabilities=[],
        ))
        second = await hub.call("local_process", "process_identity", {"value": 2})

        first_payload = json.loads(first["content"][0]["text"])
        second_payload = json.loads(second["content"][0]["text"])
        assert first_payload["pid"] == second_payload["pid"]
        assert (first_payload["value"], second_payload["value"]) == (1, 2)
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_browser_mcp_launch_material_stays_out_of_desired_state() -> None:
    source = """
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("browser-launch-test")

@mcp.tool()
def browser_snapshot() -> dict[str, object]:
    return {
        "pid": os.getpid(),
        "endpoint": os.environ.get("FLOWORK_PLAYWRIGHT_CDP_ENDPOINT"),
        "has_bearer": bool(os.environ.get("FLOWORK_PLAYWRIGHT_CDP_BEARER")),
    }

mcp.run(transport="stdio")
"""
    server = McpDesiredServer.model_validate({
        "id": "builtin:browser",
        "source": "builtin_local",
        "name": "browser",
        "configurationRevision": "browser-rev-1",
        "required": True,
        "activation": "command",
        "connection": {
            "kind": "stdio",
            "command": sys.executable,
            "args": ["-c", source],
            "cwd": "/tmp",
            "environmentProfile": "browser-gateway",
        },
    })
    operations: list[str] = []

    async def host_gateway(operation, _server, _tool_name, _arguments):
        operations.append(operation)
        assert operation == "launch"
        return {
            "environment": {
                "FLOWORK_PLAYWRIGHT_CDP_ENDPOINT": (
                    "wss://browser-host.invalid/cdp"
                ),
                "FLOWORK_PLAYWRIGHT_CDP_BEARER": "turn-browser-capability",
            }
        }

    desired = _desired(server)
    assert "turn-browser-capability" not in desired.model_dump_json()
    assert "browser-host.invalid" not in desired.model_dump_json()
    adapter = SandboxMcpRuntimeAdapter(host_gateway)
    hub = SandboxMcpHub(adapter)
    try:
        await hub.reconcile(desired)
        await hub.activate(_context(platform_capabilities=["browser"]))
        result = await hub.call("browser", "browser_snapshot", {})
        payload = json.loads(result["content"][0]["text"])
        await hub.deactivate()
        await hub.activate(_context(
            turn_id="turn-2",
            platform_capabilities=["browser"],
        ))
        second = await hub.call("browser", "browser_snapshot", {})
        second_payload = json.loads(second["content"][0]["text"])

        assert payload["has_bearer"] is True
        assert payload["endpoint"].startswith("ws://127.0.0.1:")
        assert second_payload["pid"] == payload["pid"]
        assert second_payload["endpoint"] == payload["endpoint"]
        assert operations == ["launch", "launch"]
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_remote_streamable_session_is_owned_and_reused_by_sandbox_hub() -> None:
    server = McpDesiredServer.model_validate({
        "id": "installation:remote-1",
        "source": "custom_remote",
        "name": "remote_tools",
        "configurationRevision": "remote-rev-1",
        "required": False,
        "activation": "selected",
        "connection": {
            "kind": "host_broker",
            "transport": "streamable_http",
            "brokerRoute": "runtime-mcp:remote-1",
        },
    })
    requests: list[tuple[str, str | None]] = []

    async def host_gateway(operation, _server, _tool_name, arguments):
        if operation == "remote_close":
            requests.append((operation, arguments.get("session_id")))
            return {"messages": [], "session_id": arguments.get("session_id")}
        assert operation == "remote_message"
        message = arguments["message"]
        method = str(message.get("method") or "")
        requests.append((method, arguments.get("session_id")))
        if method == "notifications/initialized":
            return {"session_id": "remote-session", "messages": []}
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "remote-test", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{
                    "name": "remote_echo",
                    "description": "Echo through the Host Broker",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                    },
                }]
            }
        elif method == "tools/call":
            value = message["params"]["arguments"]["value"]
            result = {
                "content": [{"type": "text", "text": f"value={value}"}],
                "structuredContent": {"value": value},
                "isError": False,
            }
        else:
            raise AssertionError(method)
        return {
            "session_id": "remote-session",
            "messages": [{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": result,
            }],
        }

    adapter = SandboxMcpRuntimeAdapter(host_gateway)
    hub = SandboxMcpHub(adapter)
    try:
        await hub.reconcile(_desired(server))
        await hub.activate(_context(platform_capabilities=[]))
        first = await hub.call("remote_tools", "remote_echo", {"value": 1})
        await hub.deactivate()
        await hub.reconcile(_desired(server))
        await hub.activate(_context(
            turn_id="turn-2",
            platform_capabilities=[],
        ))
        second = await hub.call("remote_tools", "remote_echo", {"value": 2})

        assert first["structured_content"] == {"value": 1}
        assert second["structured_content"] == {"value": 2}
        assert sum(method == "initialize" for method, _ in requests) == 1
        assert requests[-1] == ("tools/call", "remote-session")
    finally:
        await hub.close()
    assert requests[-1] == ("remote_close", "remote-session")


@pytest.mark.asyncio
async def test_codex_uses_one_standard_mcp_endpoint_for_the_aggregate_hub() -> None:
    calls: list[tuple[str, str | None, dict[str, Any]]] = []

    async def host_gateway(operation, _server, tool_name, arguments):
        calls.append((operation, tool_name, arguments))
        if operation == "manifest":
            return {
                "tools": [{
                    "name": "example_tool",
                    "description": "Aggregate endpoint tool",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }]
            }
        return {
            "content": [{"type": "text", "text": "aggregate-done"}],
            "structured_content": {"value": arguments["value"]},
            "is_error": False,
        }

    server = _server("platform:config")
    adapter = SandboxMcpRuntimeAdapter(host_gateway)
    hub = SandboxMcpHub(adapter)
    gateway = CodexMcpHubGateway(hub, adapter)
    await hub.reconcile(_desired(server))
    await hub.activate(_context())
    try:
        catalog = await gateway.activate(
            desired_servers=[server],
            request_approval=(
                lambda *_args: pytest.fail("approval was not expected")
            ),
            requires_approval=lambda *_args: False,
        )
        assert gateway.url is not None
        assert catalog[0]["loaded"] is True
        async with AsyncExitStack() as stack:
            streams = await stack.enter_async_context(
                streamable_http_client(gateway.url)
            )
            session = await stack.enter_async_context(
                ClientSession(streams[0], streams[1])
            )
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool("example_tool", {"value": 9})

        assert [tool.name for tool in listed.tools] == ["example_tool"]
        assert result.structuredContent == {"value": 9}
        assert calls == [
            ("manifest", None, {}),
            ("call", "example_tool", {"value": 9}),
        ]
    finally:
        await gateway.close()
        await hub.close()


def test_runtime_turn_separates_host_authority_from_sandbox_hub_contracts() -> None:
    desired = _desired(_server("platform:config"))
    context = _context()
    common = {
        "tenant_id": "tenant",
        "user_id": "user",
        "chat_id": "chat",
        "turn_id": "turn",
        "runtime_type": "codex",
        "runtime_session_id": "runtime",
        "runtime_root": "/runtime/.codex",
        "message": {"role": "user", "content": "hello"},
        "model": {"id": "gpt-test", "connection_type": "chatgpt_account"},
        "active_platform_mcps": ["config"],
        "mcp_desired_state": desired,
        "mcp_execution_context": context,
    }

    hub_request = RuntimeTurnRequest(**common, mcp_runtime_stage="sandbox")
    assert hub_request.mcp_host_servers == []

    with pytest.raises(ValidationError, match="Host-stage"):
        RuntimeTurnRequest(**common)

    with pytest.raises(ValidationError, match="cannot carry Host authority"):
        RuntimeTurnRequest(
            **common,
            mcp_runtime_stage="sandbox",
            mcp_host_servers=[{
                "name": "config",
                "source": "platform",
                "connection": {
                    "transport": "host_gateway",
                    "capability": "private",
                },
            }],
        )


def test_host_projection_removes_authority_urls_headers_and_env() -> None:
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "/diagram /document"},
        model={"id": "gpt-test", "connection_type": "chatgpt_account"},
        active_platform_mcps=["config", "diagram", "document"],
        mcp_config_revision=8,
        mcp_host_servers=[
            {
                "name": "config",
                "source": "platform",
                "connection": {
                    "transport": "host_gateway",
                    "capability": "platform-secret",
                },
            },
            {
                "name": "github",
                "source": "custom",
                "server_id": "github-installation",
                "config_revision": "github-rev-2",
                "connection": {
                    "transport": "streamable_http",
                    "url": "http://platform.internal/runtime-mcp/v1/github",
                    "headers": {"Authorization": "Bearer remote-secret"},
                },
            },
            {
                "name": "local_docs",
                "source": "custom",
                "server_id": "local-installation",
                "config_revision": "local-rev-1",
                "connection": {
                    "transport": "stdio",
                    "command": "local-docs-mcp",
                    "args": ["--root", "/data/docs"],
                    "cwd": "/data",
                },
            },
        ],
    )

    desired, context = build_mcp_lifecycle_contracts(
        request,
        sandbox_id="sandbox-1",
        sandbox_generation=4,
        authorization_generation="auth-generation-1",
        execution_capability="shadow-only-capability",
        lifetime_s=300,
    )

    encoded = desired.model_dump_json(by_alias=True)
    assert "platform-secret" not in encoded
    assert "remote-secret" not in encoded
    assert "platform.internal" not in encoded
    assert '"headers"' not in encoded
    assert '"env"' not in encoded
    by_name = {server.name: server for server in desired.servers}
    assert by_name["diagram"].source == "builtin_local"
    assert by_name["document"].source == "builtin_local"
    assert by_name["document"].connection.command == "flowork-document-mcp"
    assert "browser" not in by_name
    assert by_name["github"].source == "custom_remote"
    assert by_name["github"].connection.broker_route == (
        "runtime-mcp:github-installation"
    )
    assert by_name["local_docs"].source == "custom_stdio"
    assert context.selected_mcp_revision == 8
    assert set(context.active_platform_capabilities) == {
        "config",
        "diagram",
        "document",
    }


def test_sandbox_manager_replaces_host_authority_after_epoch_is_known() -> None:
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={"id": "gpt-test", "connection_type": "chatgpt_account"},
        active_platform_mcps=["config"],
        mcp_config_revision=3,
        mcp_host_servers=[{
            "name": "config",
            "source": "platform",
            "connection": {
                "transport": "host_gateway",
                "capability": "host-secret",
            },
        }],
    )
    session = object.__new__(SandboxSession)
    session._sandbox_runtime_id = "sandbox-epoch-owner"
    session._runtime_process_generation = 9
    projected = RuntimeTurnRequest.model_validate(
        session._mcp_runtime_request(request.model_dump(mode="json"))
    )

    assert projected.mcp_runtime_stage == "sandbox"
    assert projected.mcp_desired_state is not None
    assert projected.mcp_desired_state.sandbox_id == "sandbox-epoch-owner"
    assert projected.mcp_desired_state.sandbox_generation == 9
    assert projected.mcp_execution_context is not None
    assert projected.mcp_execution_context.sandbox_generation == 9
    assert (
        projected.mcp_execution_context.capability.get_secret_value()
        != "**********"
    )
    assert verify_mcp_execution_capability(
        projected.mcp_execution_context.capability.get_secret_value(),
        secret=config.signing_secret,
    ) is not None
    assert "host-secret" not in projected.mcp_desired_state.model_dump_json()

    assert projected.mcp_host_servers == []
    assert {server.name for server in projected.mcp_desired_state.servers} == {
        "config",
        "interactive",
    }
