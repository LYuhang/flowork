"""Runtime-neutral MCP client session factory used inside sandboxes."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


@asynccontextmanager
async def mcp_client_session(
    connection: dict[str, Any],
) -> AsyncIterator[ClientSession]:
    """Open and initialize one official MCP SDK client session."""
    transport = str(connection.get("transport") or "").replace("-", "_")
    async with AsyncExitStack() as stack:
        if transport == "stdio":
            read, write = await stack.enter_async_context(stdio_client(
                StdioServerParameters(
                    command=str(connection.get("command") or ""),
                    args=[str(value) for value in connection.get("args") or []],
                    env=connection.get("env"),
                    cwd=connection.get("cwd"),
                )
            ))
        elif transport == "sse":
            read, write = await stack.enter_async_context(sse_client(
                str(connection.get("url") or ""),
                headers=dict(connection.get("headers") or {}),
                timeout=float(connection.get("timeout") or 5),
                sse_read_timeout=float(
                    connection.get("sse_read_timeout") or 300
                ),
            ))
        elif transport in {"http", "streamable_http"}:
            headers = dict(connection.get("headers") or {})
            client = await stack.enter_async_context(httpx.AsyncClient(
                headers=headers,
                timeout=float(connection.get("timeout") or 30),
            ))
            streams = await stack.enter_async_context(streamable_http_client(
                str(connection.get("url") or ""),
                http_client=client,
                terminate_on_close=bool(
                    connection.get("terminate_on_close", True)
                ),
            ))
            read, write = streams[0], streams[1]
        else:
            raise ValueError(f"unsupported MCP transport: {transport!r}")

        session = await stack.enter_async_context(ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(
                seconds=float(connection.get("timeout") or 60)
            ),
        ))
        await session.initialize()
        yield session


__all__ = ["mcp_client_session"]
