"""Executable adapters owned by the sandbox-local MCP Hub.

The adapter deliberately has no platform database, authorization, or secret
service imports. Platform tools cross the private Runtime bus to the trusted
Host Gateway. Credential-free stdio servers are supervised directly by the
resident Chat Runtime and retain their protocol session across Turns.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import anyio
from mcp import ClientSession
from mcp import types
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import TypeAdapter
from mcp.shared.message import SessionMessage

from vibecanvas_api.agents.tools import builtin_tool_names
from vibecanvas_api.browser.playwright_contract import filter_playwright_tools
from vibecanvas_api.config import config

from .mcp_runtime_protocol import (
    McpDesiredServer,
    McpExecutionContext,
    McpStdioLaunch,
)
from .mcp_browser_transport import (
    BrowserCdpRelay,
    start_browser_cdp_relay,
)


_CONTENT_BLOCK = TypeAdapter(types.ContentBlock)
_JSONRPC_MESSAGE = TypeAdapter(types.JSONRPCMessage)


McpGatewayCallback = Callable[
    [str, McpDesiredServer, str | None, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


@dataclass(slots=True)
class _LocalSession:
    stack: AsyncExitStack
    session: ClientSession
    browser_relay: BrowserCdpRelay | None = None


@dataclass(slots=True)
class _RemoteSession:
    stack: AsyncExitStack
    session: ClientSession
    owner_task: asyncio.Task[None]
    session_state: dict[str, str]


@dataclass(frozen=True, slots=True)
class HubToolProjection:
    server_name: str
    upstream_name: str
    tool: types.Tool


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return _jsonable(
            value.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    return str(value)


def _tool_manifest(tool: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(tool, "name", "") or ""),
        "description": str(getattr(tool, "description", "") or ""),
        "inputSchema": _jsonable(
            getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None)
            or {"type": "object", "properties": {}}
        ),
        "outputSchema": _jsonable(
            getattr(tool, "outputSchema", None)
            or getattr(tool, "output_schema", None)
        ),
        "annotations": _jsonable(getattr(tool, "annotations", None)),
    }


class SandboxMcpRuntimeAdapter:
    """Run local MCP sessions and bridge privileged facades to the Host."""

    def __init__(self, gateway: McpGatewayCallback) -> None:
        self._gateway = gateway
        self._local: dict[str, _LocalSession] = {}
        self._remote: dict[str, _RemoteSession] = {}
        self._manifests: dict[str, tuple[dict[str, Any], ...]] = {}
        self._lock = asyncio.Lock()

    def set_gateway(self, gateway: McpGatewayCallback) -> None:
        """Replace only the Turn-local Host transport callback."""
        self._gateway = gateway

    async def start(self, server: McpDesiredServer) -> tuple[str, ...]:
        if server.connection.kind == "platform_facade":
            response = await self._gateway("manifest", server, None, {})
            tools = response.get("tools")
            if not isinstance(tools, list):
                raise RuntimeError(
                    f"Host Gateway returned no manifest for {server.name}"
                )
            manifest = tuple(
                dict(item) for item in tools if isinstance(item, dict)
            )
        elif server.connection.kind == "stdio":
            manifest = await self._start_stdio(server, server.connection)
        else:
            manifest = await self._start_remote(server)
        names = tuple(str(item.get("name") or "") for item in manifest)
        if not names or any(not name for name in names):
            await self.stop(server)
            raise RuntimeError(f"MCP server {server.name!r} has no valid tools")
        if len(names) != len(set(names)):
            await self.stop(server)
            raise RuntimeError(
                f"MCP server {server.name!r} exports duplicate tool names"
            )
        self._manifests[server.id] = manifest
        return names

    async def _start_stdio(
        self,
        server: McpDesiredServer,
        launch: McpStdioLaunch,
    ) -> tuple[dict[str, Any], ...]:
        async with self._lock:
            if server.id in self._local:
                raise RuntimeError(f"MCP server {server.id!r} is already running")
            stack = AsyncExitStack()
            browser_relay: BrowserCdpRelay | None = None
            try:
                child_env: dict[str, str] | None = None
                if launch.environment_profile == "browser-gateway":
                    local_bearer = secrets.token_urlsafe(32)
                    browser_relay = await start_browser_cdp_relay(
                        local_bearer=local_bearer,
                    )
                    playwright_home = "/tmp/flowork-playwright-mcp"
                    playwright_cache = f"{playwright_home}/cache"
                    os.makedirs(playwright_cache, mode=0o700, exist_ok=True)
                    child_env = {
                        "HOME": playwright_home,
                        "XDG_CACHE_HOME": playwright_cache,
                        "TMPDIR": "/tmp",
                        "FLOWORK_PLAYWRIGHT_CDP_ENDPOINT": (
                            browser_relay.endpoint
                        ),
                        "FLOWORK_PLAYWRIGHT_CDP_BEARER": local_bearer,
                    }
                read, write = await stack.enter_async_context(stdio_client(
                    StdioServerParameters(
                        command=launch.command,
                        args=list(launch.args),
                        cwd=launch.cwd,
                        env=child_env,
                    )
                ))
                session = await stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                listed = await session.list_tools()
            except BaseException:
                await stack.aclose()
                if browser_relay is not None:
                    await browser_relay.close()
                raise
            self._local[server.id] = _LocalSession(
                stack=stack,
                session=session,
                browser_relay=browser_relay,
            )
        return tuple(_tool_manifest(tool) for tool in listed.tools)

    async def _start_remote(
        self,
        server: McpDesiredServer,
    ) -> tuple[dict[str, Any], ...]:
        if server.connection.kind != "host_broker":
            raise TypeError("remote MCP requires a Host Broker connection")
        if server.connection.transport != "streamable_http":
            raise RuntimeError("mcp_remote_sse_transport_not_ready")
        async with self._lock:
            if server.id in self._remote:
                raise RuntimeError(f"MCP server {server.id!r} is already running")
            server_send, client_read = anyio.create_memory_object_stream[
                SessionMessage | Exception
            ](32)
            client_write, server_receive = anyio.create_memory_object_stream[
                SessionMessage
            ](32)
            session_state: dict[str, str] = {}

            async def own_transport() -> None:
                try:
                    async with server_send, server_receive:
                        async for session_message in server_receive:
                            serialized_message = session_message.message.model_dump(
                                mode="json",
                                by_alias=True,
                                exclude_none=True,
                            )
                            if serialized_message.get("method") == "initialize":
                                params = serialized_message.get("params")
                                if isinstance(params, dict):
                                    requested_protocol = params.get(
                                        "protocolVersion"
                                    )
                                    if isinstance(requested_protocol, str):
                                        session_state["protocol_version"] = (
                                            requested_protocol
                                        )
                            response = await self._gateway(
                                "remote_message",
                                server,
                                None,
                                {
                                    "message": serialized_message,
                                    "session_id": session_state.get("id"),
                                    "protocol_version": session_state.get(
                                        "protocol_version"
                                    ),
                                },
                            )
                            session_id = response.get("session_id")
                            if isinstance(session_id, str) and session_id:
                                session_state["id"] = session_id
                            messages = response.get("messages")
                            if not isinstance(messages, list):
                                raise RuntimeError(
                                    "Host Broker returned invalid MCP messages"
                                )
                            for item in messages:
                                result = item.get("result")
                                if isinstance(result, dict):
                                    negotiated = result.get("protocolVersion")
                                    if isinstance(negotiated, str):
                                        session_state["protocol_version"] = negotiated
                                message = _JSONRPC_MESSAGE.validate_python(item)
                                await server_send.send(SessionMessage(message))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    with anyio.move_on_after(1):
                        await server_send.send(exc)

            owner_task = asyncio.create_task(own_transport())
            stack = AsyncExitStack()
            try:
                session = await stack.enter_async_context(
                    ClientSession(client_read, client_write)
                )
                await session.initialize()
                listed = await session.list_tools()
            except BaseException:
                await stack.aclose()
                owner_task.cancel()
                await asyncio.gather(owner_task, return_exceptions=True)
                raise
            self._remote[server.id] = _RemoteSession(
                stack=stack,
                session=session,
                owner_task=owner_task,
                session_state=session_state,
            )
        return tuple(_tool_manifest(tool) for tool in listed.tools)

    async def stop(self, server: McpDesiredServer) -> None:
        self._manifests.pop(server.id, None)
        async with self._lock:
            local = self._local.pop(server.id, None)
            remote = self._remote.pop(server.id, None)
        if local is not None:
            await local.stack.aclose()
            if local.browser_relay is not None:
                await local.browser_relay.close()
        if remote is not None:
            await remote.stack.aclose()
            remote.owner_task.cancel()
            await asyncio.gather(remote.owner_task, return_exceptions=True)
            try:
                await self._gateway(
                    "remote_close",
                    server,
                    None,
                    {"session_id": remote.session_state.get("id")},
                )
            except Exception:
                pass

    async def call(
        self,
        server: McpDesiredServer,
        tool_name: str,
        arguments: dict[str, Any],
        execution_context: McpExecutionContext,
    ) -> Any:
        del execution_context  # The Hub validates and owns the live context.
        if server.connection.kind == "platform_facade":
            return await self._gateway(
                "call",
                server,
                tool_name,
                dict(arguments),
            )
        if server.connection.kind == "host_broker":
            remote = self._remote.get(server.id)
            if remote is None:
                raise RuntimeError(f"MCP server {server.name!r} is not connected")
            result = await remote.session.call_tool(tool_name, dict(arguments))
            return {
                "content": _jsonable(result.content),
                "structured_content": _jsonable(result.structuredContent),
                "is_error": bool(result.isError),
            }
        local = self._local.get(server.id)
        if local is None:
            raise RuntimeError(f"MCP server {server.name!r} is not connected")
        result = await local.session.call_tool(tool_name, dict(arguments))
        return {
            "content": _jsonable(result.content),
            "structured_content": _jsonable(result.structuredContent),
            "is_error": bool(result.isError),
        }

    async def activate(
        self,
        server: McpDesiredServer,
        execution_context: McpExecutionContext,
    ) -> None:
        del execution_context
        if (
            server.connection.kind != "stdio"
            or server.connection.environment_profile != "browser-gateway"
        ):
            return
        local = self._local.get(server.id)
        if local is None or local.browser_relay is None:
            raise RuntimeError("Browser MCP relay is not running")
        response = await self._gateway("launch", server, None, {})
        environment = response.get("environment")
        if not isinstance(environment, dict):
            raise RuntimeError(
                "Host Gateway returned no Browser launch environment"
            )
        await local.browser_relay.activate(
            upstream_url=str(
                environment.get("FLOWORK_PLAYWRIGHT_CDP_ENDPOINT") or ""
            ),
            upstream_bearer=str(
                environment.get("FLOWORK_PLAYWRIGHT_CDP_BEARER") or ""
            ),
        )

    async def deactivate(self, server: McpDesiredServer) -> None:
        if (
            server.connection.kind != "stdio"
            or server.connection.environment_profile != "browser-gateway"
        ):
            return
        local = self._local.get(server.id)
        if local is not None and local.browser_relay is not None:
            await local.browser_relay.deactivate()

    def manifest(self, server_id: str) -> tuple[dict[str, Any], ...]:
        return self._manifests.get(server_id, ())


def hub_call_result(value: Any) -> types.CallToolResult:
    if isinstance(value, types.CallToolResult):
        return value
    if not isinstance(value, dict):
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(value))]
        )
    raw_content = value.get("content")
    raw_content = raw_content if isinstance(raw_content, list) else []
    content = [
        _CONTENT_BLOCK.validate_python(item)
        for item in raw_content
        if isinstance(item, dict)
    ]
    if not content:
        content = [types.TextContent(type="text", text="")]
    return types.CallToolResult(
        content=content,
        structuredContent=(
            value.get("structured_content")
            if isinstance(value.get("structured_content"), dict)
            else None
        ),
        isError=bool(value.get("is_error")),
    )


async def project_hub_tools(
    hub,
    adapter: SandboxMcpRuntimeAdapter,
    desired_servers: list[McpDesiredServer],
) -> tuple[list[HubToolProjection], list[dict[str, Any]]]:
    """Apply the shared model-facing MCP naming and budget policy."""
    status = await hub.status()
    status_by_id = {item.id: item for item in status.servers}
    reserved = builtin_tool_names()
    seen = set(reserved)
    direct_mcp_names: set[str] = set()
    tenant_cap = int(config.mcp.per_tenant_tool_cap)
    server_cap = int(config.mcp.per_server_tool_cap)
    projections: list[HubToolProjection] = []
    catalog: list[dict[str, Any]] = []
    server_list = sorted(
        desired_servers,
        key=lambda item: (
            0 if item.source in {"platform", "builtin_local"} else 1,
            item.name,
        ),
    )
    for server in server_list:
        seen_before = set(seen)
        direct_names_before = set(direct_mcp_names)
        server_status = status_by_id.get(server.id)
        manifest = adapter.manifest(server.id)
        projected: list[Any] = []
        error: str | None = None
        if server_status is None or server_status.state != "ready":
            error = (
                server_status.last_error_code
                if server_status is not None
                else "mcp_server_missing_from_hub"
            )
        try:
            if error is not None:
                raise RuntimeError(error)
            if len(manifest) > server_cap:
                raise RuntimeError(
                    f"server exported {len(manifest)} tools; limit is {server_cap}"
                )
            if len(projections) + len(manifest) > tenant_cap:
                raise RuntimeError(
                    f"tenant MCP tool limit {tenant_cap} would be exceeded"
                )
            mcp_tools = [types.Tool.model_validate(item) for item in manifest]
            if server.name == "browser":
                mcp_tools = filter_playwright_tools(mcp_tools)
            for mcp_tool in mcp_tools:
                raw_name = str(mcp_tool.name or "")
                if not raw_name:
                    raise RuntimeError("server exported a tool without a name")
                if server.source in {"platform", "builtin_local"}:
                    if raw_name in direct_mcp_names:
                        raise RuntimeError(
                            f"duplicate MCP tool name: {raw_name}"
                        )
                    reserved.discard(raw_name)
                    seen.discard(raw_name)
                    direct_mcp_names.add(raw_name)
                    qualified = raw_name
                else:
                    qualified = f"{server.name}__{raw_name}"
                if qualified in seen:
                    raise RuntimeError(
                        f"duplicate MCP tool name: {qualified}"
                    )
                seen.add(qualified)
                projected.append(HubToolProjection(
                    server_name=server.name,
                    upstream_name=raw_name,
                    tool=mcp_tool.model_copy(update={"name": qualified}),
                ))
            projections.extend(projected)
        except Exception as exc:
            seen = seen_before
            direct_mcp_names = direct_names_before
            if server.required:
                raise RuntimeError(
                    f"required MCP {server.name} could not be projected: {exc}"
                ) from exc
            error = str(exc)
            projected = []
        catalog.append({
            "name": server.name,
            "server_id": server.id,
            "description": server.description,
            "loaded": error is None,
            "source": (
                "platform"
                if server.source in {"platform", "builtin_local"}
                else "custom"
            ),
            "tool_count": len(projected),
            "health": "ready" if error is None else "degraded",
            "cache_status": "hub",
            "handshake_ms": 0,
            "retry_count": 0,
            "config_revision": server.configuration_revision,
            "tools": (
                [item.tool.model_dump(mode="json", by_alias=True) for item in projected]
                if error is None
                else []
            ),
            **({"error": error} if error is not None else {}),
        })
    return projections, catalog


__all__ = [
    "HubToolProjection",
    "SandboxMcpRuntimeAdapter",
    "hub_call_result",
    "project_hub_tools",
]
