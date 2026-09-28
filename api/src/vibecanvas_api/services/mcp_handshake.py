"""Single MCP server handshake through the configured one-shot OS sandbox.

Used by MCP management probes at
agent-build time AND by the routes that need to probe a server up-front
(MCP T5 ``POST /mcp-servers/test``, MCP T6 ``POST
/mcp-servers/{id}/refresh``, and PATCH endpoint when the endpoint /
transport changes).

Contract — always returns a dict, never raises:

    {
        "status":     "ok" | "error: <msg>",
        "tool_count": int | None,            # None on failure
        "tool_names": list[{name, description}] | None,
        "tools":      list[BaseTool],        # empty list on failure
    }

The non-raising shape is load-bearing: the loader iterates all servers
and per-server try/except is *belt and braces* — even if a future
refactor drops it, a single broken server can never abort the whole
load. ``tools`` is only populated on success; the route handlers
discard it (they only persist the count + names snapshot via
``McpServersRepo.update_handshake``).
"""
from __future__ import annotations

import asyncio
from typing import Any

from vibecanvas_api.services.mcp_config import (
    build_connection_config,
    validate_mcp_connection_destination,
)
from vibecanvas_api.services.public_url import PublicUrlError
from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox import get_sandbox_provider
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager


def _exception_message(exc: BaseException) -> str:
    children = getattr(exc, "exceptions", None)
    if isinstance(children, tuple):
        messages = [_exception_message(child) for child in children]
        return "; ".join(message for message in messages if message)
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


async def handshake_one(
    *,
    prefix: str,
    transport: str,
    endpoint: str,
    auth_config: dict,
    connection_config: dict | None = None,
    timeout_s: float,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Probe a single MCP server and return its tool list.

    Always-returns-dict contract (see module docstring). ``timeout_s``
    is the *total* wait for ``get_tools()`` — the underlying client may
    do connect + initialize + list, but the caller only sees one bound.
    """
    try:
        connection = build_connection_config(
            transport=transport,
            endpoint=endpoint,
            auth_config=auth_config,
            connection_config=connection_config,
        )
        allow_hosts = await validate_mcp_connection_destination(connection)

        request = {
            "prefix": prefix,
            "connection": connection,
            "timeout_s": timeout_s,
        }
        if config.sandbox_service_mode == "service":
            if not tenant_id:
                raise RuntimeError("tenant scope is required for an MCP probe")
            result = await get_sandbox_manager().run_mcp_probe(
                str(tenant_id),
                request,
                timeout=timeout_s,
                allow_hosts=sorted(allow_hosts),
            )
        else:
            # Isolated unit tests retain the embedded provider seam. Normal
            # application deployments cannot execute this branch.
            provider = get_sandbox_provider(trust="untrusted")
            result = await asyncio.to_thread(
                provider.run_mcp_probe,
                request=request,
                timeout=timeout_s,
                allow_hosts=allow_hosts,
            )
        return {
            "status": str(result.get("status") or "error: malformed probe result"),
            "tool_count": result.get("tool_count"),
            "tool_names": result.get("tool_names"),
            # Probe routes persist only the serializable manifest. Runtime MCP
            # tools are built separately inside the owning Chat sandbox.
            "tools": [],
        }
    except PublicUrlError as exc:
        return {
            "status": f"error: {exc}",
            "tool_count": None,
            "tool_names": None,
            "tools": [],
            "security_rejected": True,
        }
    except Exception as exc:
        return {
            "status": f"error: {_exception_message(exc)}",
            "tool_count": None,
            "tool_names": None,
            "tools": [],
        }
