"""One stable loopback MCP endpoint backed by the sandbox-owned aggregate Hub."""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from mcp import types
from mcp.server.fastmcp import FastMCP

from .mcp_hub_adapter import (
    SandboxMcpRuntimeAdapter,
    hub_call_result,
    project_hub_tools,
)
from .mcp_runtime_protocol import McpDesiredServer


ApprovalCallback = Callable[[str, dict[str, Any], str], Awaitable[str]]
ApprovalPredicate = Callable[[str, dict[str, Any]], bool]


def _not_executed_result(tool_name: str, action: str) -> types.CallToolResult:
    reason = "user_cancelled" if action == "cancel" else "user_denied"
    message = "Tool call was not executed because user approval was not granted."
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        structuredContent={
            "status": "error",
            "error": {"code": reason, "message": message},
            "tool": tool_name,
            "not_executed": True,
        },
        isError=True,
    )


def _inactive_result(tool_name: str) -> types.CallToolResult:
    message = "The MCP Hub is not active for an Agent Turn."
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        structuredContent={
            "status": "error",
            "error": {"code": "runtime_turn_inactive", "message": message},
            "tool": tool_name,
            "not_executed": True,
        },
        isError=True,
    )


class CodexMcpHubGateway:
    """Expose the complete sandbox Hub to Codex through one resident URL."""

    def __init__(self, hub, adapter: SandboxMcpRuntimeAdapter) -> None:
        self._hub = hub
        self._adapter = adapter
        self._tools: list[types.Tool] = []
        self._routes: dict[str, tuple[str, str]] = {}
        self._request_approval: ApprovalCallback | None = None
        self._requires_approval: ApprovalPredicate | None = None
        self._active = False
        self._mcp: FastMCP | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._session_manager_context: Any = None
        self._socket: socket.socket | None = None
        self.url: str | None = None

    async def activate(
        self,
        *,
        desired_servers: list[McpDesiredServer],
        request_approval: ApprovalCallback,
        requires_approval: ApprovalPredicate,
    ) -> list[dict[str, Any]]:
        projections, catalog = await project_hub_tools(
            self._hub,
            self._adapter,
            desired_servers,
        )
        self._tools = [item.tool for item in projections]
        self._routes = {
            item.tool.name: (item.server_name, item.upstream_name)
            for item in projections
        }
        self._request_approval = request_approval
        self._requires_approval = requires_approval
        self._active = True
        await self.start()
        return catalog

    def deactivate(self) -> None:
        self._active = False
        self._request_approval = None
        self._requires_approval = None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        if not self._active:
            return _inactive_result(name)
        route = self._routes.get(name)
        if route is None:
            return types.CallToolResult(
                content=[types.TextContent(
                    type="text",
                    text=f"Unknown MCP Hub tool: {name}",
                )],
                isError=True,
            )
        request_approval = self._request_approval
        requires_approval = self._requires_approval
        if request_approval is None or requires_approval is None:
            return _inactive_result(name)
        if requires_approval(name, arguments):
            action = await request_approval(
                name,
                dict(arguments),
                f"mcp_{uuid.uuid4().hex}",
            )
            if action != "approve":
                return _not_executed_result(name, action)
        server_name, upstream_name = route
        return hub_call_result(await self._hub.call(
            server_name,
            upstream_name,
            dict(arguments),
        ))

    async def start(self) -> None:
        if self._server_task is not None:
            return
        mcp = FastMCP(
            "flowork-sandbox-mcp-hub",
            instructions=(
                "Flowork MCP Hub. Tools are scoped to the current Chat and "
                "active Agent Turn."
            ),
            streamable_http_path="/",
            stateless_http=True,
            json_response=False,
        )
        lowlevel = mcp._mcp_server

        @lowlevel.list_tools()
        async def list_tools() -> list[types.Tool]:
            return list(self._tools) if self._active else []

        @lowlevel.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]):
            return await self.call_tool(name, arguments)

        app = mcp.streamable_http_app()
        session_context = mcp.session_manager.run()
        await session_context.__aenter__()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="off",
        ))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                    raise RuntimeError("Codex MCP Hub gateway failed to start")
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("Codex MCP Hub gateway startup timed out")
        except BaseException:
            server.should_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            sock.close()
            await session_context.__aexit__(None, None, None)
            raise
        self._mcp = mcp
        self._session_manager_context = session_context
        self._socket = sock
        self._server = server
        self._server_task = task
        self.url = f"http://127.0.0.1:{int(sock.getsockname()[1])}/"

    async def close(self) -> None:
        self.deactivate()
        server = self._server
        task = self._server_task
        session_context = self._session_manager_context
        sock = self._socket
        self._server = None
        self._server_task = None
        self._session_manager_context = None
        self._socket = None
        self._mcp = None
        self.url = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if sock is not None:
            sock.close()
        if session_context is not None:
            await session_context.__aexit__(None, None, None)


__all__ = ["CodexMcpHubGateway"]
