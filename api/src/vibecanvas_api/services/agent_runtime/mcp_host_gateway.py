"""Trusted Host Capability Gateway for the sandbox-owned MCP Runtime."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.mcp_execution_capability import (
    verify_mcp_execution_capability,
)
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeEvent,
    RuntimeMcpGatewayRequest,
    RuntimeMcpGatewayResponse,
    RuntimeTurnRequest,
)
from vibecanvas_api.services.platform_mcp.invocation import (
    invoke_platform_mcp_tool,
    platform_mcp_tool_manifest,
)


_OPERATIONS = {
    "manifest",
    "call",
    "launch",
    "remote_message",
    "remote_close",
}
_MAX_REMOTE_RESPONSE_BYTES = 8 * 1024 * 1024
logger = structlog.get_logger(__name__)


def _gateway_rejection_code(exc: Exception) -> str:
    """Map private authorization errors to non-sensitive operator signals."""
    known = {
        "invalid or expired MCP execution capability": "execution_capability_invalid",
        "MCP execution capability identity mismatch": "execution_identity_mismatch",
        "MCP execution capability scope mismatch": "execution_scope_mismatch",
    }
    message = str(exc)
    if message in known:
        return known[message]
    if message.startswith("Platform MCP ") and message.endswith(
        " is inactive for this Turn"
    ):
        return "platform_capability_inactive"
    if message.startswith("Platform MCP ") and message.endswith(
        " has no Host capability"
    ):
        return "platform_capability_missing"
    return type(exc).__name__


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


def _platform_capability_token(request: RuntimeTurnRequest, server: str) -> str:
    """Read a Host-only rollout capability never serialized to the sandbox."""
    descriptor = next(
        (
            item
            for item in request.mcp_host_servers
            if item.source == "platform" and item.name == server
        ),
        None,
    )
    if descriptor is None:
        raise PermissionError(
            f"Platform MCP {server!r} is not active for this Turn"
        )
    capability = str(descriptor.connection.get("capability") or "")
    if not capability:
        raise PermissionError(
            f"Platform MCP {server!r} has no Host capability"
        )
    return capability


def _playwright_cdp_url() -> str:
    parts = urlsplit(config.mcp.platform_internal_base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Platform MCP internal base URL must be absolute")
    return urlunsplit((
        "wss" if parts.scheme == "https" else "ws",
        parts.netloc,
        "/api/v1/browser/playwright/cdp",
        "",
        "",
    ))


def _verify_private_execution(
    gateway_request: RuntimeMcpGatewayRequest,
    request: RuntimeTurnRequest,
) -> None:
    capability = verify_mcp_execution_capability(
        gateway_request.execution_capability.get_secret_value(),
        secret=config.signing_secret,
    )
    if capability is None:
        raise PermissionError("invalid or expired MCP execution capability")
    if (
        request.tenant_id,
        request.user_id,
        request.chat_id,
        request.runtime_session_id,
        request.turn_id,
        request.mcp_config_revision,
    ) != (
        capability.organization_id,
        capability.user_id,
        capability.chat_id,
        capability.runtime_session_id,
        capability.turn_id,
        capability.selected_mcp_revision,
    ):
        raise PermissionError("MCP execution capability identity mismatch")
    if set(capability.active_platform_capabilities) != set(
        request.active_platform_mcps
    ):
        raise PermissionError("MCP execution capability scope mismatch")


def _remote_descriptor(request: RuntimeTurnRequest, server: str):
    descriptor = next(
        (
            item
            for item in request.mcp_host_servers
            if item.source == "custom" and item.name == server
        ),
        None,
    )
    if descriptor is None:
        raise PermissionError(
            f"Remote MCP {server!r} is not selected for this Turn"
        )
    if descriptor.connection.get("transport") != "streamable_http":
        raise ValueError("remote MCP Hub transport requires Streamable HTTP")
    return descriptor


def _remote_messages(content_type: str, body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    if len(body) > _MAX_REMOTE_RESPONSE_BYTES:
        raise RuntimeError("remote MCP response exceeded the size limit")
    values: list[Any] = []
    if "text/event-stream" in content_type.casefold():
        data_lines: list[str] = []
        for line in body.decode("utf-8").splitlines():
            if line == "":
                if data_lines:
                    values.append(json.loads("\n".join(data_lines)))
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            values.append(json.loads("\n".join(data_lines)))
    else:
        decoded = json.loads(body)
        values.extend(decoded if isinstance(decoded, list) else [decoded])
    return [dict(item) for item in values if isinstance(item, dict)]


async def _proxy_remote_message(
    request: RuntimeTurnRequest,
    *,
    server: str,
    arguments: dict[str, Any],
    close: bool,
) -> dict[str, Any]:
    descriptor = _remote_descriptor(request, server)
    connection = descriptor.connection
    session_id = str(arguments.get("session_id") or "")
    protocol_version = str(
        arguments.get("protocol_version") or "2025-06-18"
    )
    if (
        any(char in session_id for char in "\r\n")
        or any(char in protocol_version for char in "\r\n")
        or len(protocol_version) > 64
    ):
        raise ValueError("remote MCP protocol headers are invalid")
    headers = {
        **{
            str(key): str(value)
            for key, value in dict(connection.get("headers") or {}).items()
        },
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol_version,
    }
    if session_id:
        headers["MCP-Session-Id"] = session_id
    message = arguments.get("message")
    if not close and not isinstance(message, dict):
        raise ValueError("remote MCP message is missing")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,
            read=24 * 60 * 60,
            write=60.0,
            pool=10.0,
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.request(
            "DELETE" if close else "POST",
            str(connection.get("url") or ""),
            headers=headers,
            json=None if close else message,
        )
    if 300 <= response.status_code < 400:
        raise RuntimeError("remote MCP broker redirect was denied")
    if response.status_code >= 400:
        raise RuntimeError(
            f"remote MCP broker returned HTTP {response.status_code}"
        )
    response_session_id = str(response.headers.get("mcp-session-id") or "")
    return {
        "session_id": response_session_id or session_id or None,
        "messages": _remote_messages(
            str(response.headers.get("content-type") or ""),
            response.content,
        ),
    }


async def handle_mcp_gateway_request(
    event: RuntimeEvent,
    request: RuntimeTurnRequest,
) -> RuntimeMcpGatewayResponse:
    """Authorize and execute one private request from the sandbox Hub."""
    gateway_request = RuntimeMcpGatewayRequest.model_validate(event.payload)
    request_id = gateway_request.request_id
    operation = gateway_request.operation
    server = gateway_request.server
    correlation = gateway_request.runtime_correlation
    try:
        if not request_id or correlation.source != "mcp_hub":
            raise ValueError("invalid MCP Hub gateway correlation")
        _verify_private_execution(gateway_request, request)
        if operation not in _OPERATIONS:
            raise ValueError(f"unsupported MCP Gateway operation: {operation}")
        if operation in {"remote_message", "remote_close"}:
            arguments = gateway_request.arguments
            result_payload = await _proxy_remote_message(
                request,
                server=server,
                arguments=arguments,
                close=operation == "remote_close",
            )
        elif server not in request.active_platform_mcps:
            raise PermissionError(
                f"Platform MCP {server!r} is inactive for this Turn"
            )
        elif operation == "manifest":
            manifest = platform_mcp_tool_manifest(server)
            result_payload = {
                "server": server,
                "tools": [_jsonable(tool) for tool in manifest],
            }
        elif operation == "launch":
            if server != "browser":
                raise ValueError("launch material is only defined for Browser")
            result_payload = {
                "server": server,
                "environment": {
                    "FLOWORK_PLAYWRIGHT_CDP_ENDPOINT": _playwright_cdp_url(),
                    "FLOWORK_PLAYWRIGHT_CDP_BEARER": (
                        _platform_capability_token(request, server)
                    ),
                },
            }
        else:
            tool_name = str(gateway_request.tool_name or "")
            arguments = gateway_request.arguments
            result = await invoke_platform_mcp_tool(
                server=server,
                tool_name=tool_name,
                arguments=arguments,
                capability_token=_platform_capability_token(request, server),
            )
            if isinstance(result, tuple):
                content, structured = result
                result_payload = {
                    "server": server,
                    "tool_name": tool_name,
                    "content": _jsonable(content),
                    "structured_content": _jsonable(structured),
                    "is_error": False,
                }
            else:
                result_payload = {
                    "server": server,
                    "tool_name": tool_name,
                    "content": _jsonable(result.content),
                    "structured_content": _jsonable(result.structuredContent),
                    "is_error": bool(result.isError),
                }
        return RuntimeMcpGatewayResponse(
            request_id=request_id,
            chat_id=request.chat_id,
            turn_id=request.turn_id,
            operation=operation,
            action="accepted",
            payload=result_payload,
            correlation=correlation,
        )
    except Exception as exc:  # noqa: BLE001 - private protocol rejection
        logger.warning(
            "sandbox_mcp_gateway_request_rejected",
            operation=operation,
            server=server,
            tool_name=str(gateway_request.tool_name or ""),
            error_type=type(exc).__name__,
            rejection_code=_gateway_rejection_code(exc),
            error=str(exc)[:500],
        )
        return RuntimeMcpGatewayResponse(
            request_id=request_id or "mcp_gateway_request",
            chat_id=request.chat_id,
            turn_id=request.turn_id,
            operation=(operation if operation in _OPERATIONS else "call"),
            action="rejected",
            error=str(exc),
            correlation=correlation,
        )


__all__ = ["handle_mcp_gateway_request"]
