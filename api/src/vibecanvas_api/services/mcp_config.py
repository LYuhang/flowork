"""MCP connection normalization shared by routes, probes, and sandbox jobs."""
from __future__ import annotations

from typing import Any

from vibecanvas_api.config import config
from vibecanvas_api.services.public_url import validate_public_http_url

HTTP_TRANSPORTS = {"http", "streamable_http", "streamable-http", "sse"}
ALLOWED_TRANSPORTS = {"stdio", "sse", "streamable_http", "streamable-http", "http"}


def mcp_headers(auth_config: dict | None) -> dict[str, str]:
    auth = auth_config or {}
    if auth.get("type") == "bearer" and auth.get("token"):
        return {"Authorization": f"Bearer {auth['token']}"}
    return {}


def normalize_transport(value: str) -> str:
    transport = (value or "").strip()
    if transport == "streamable-http":
        return "streamable_http"
    return transport


def build_connection_config(
    *,
    transport: str,
    endpoint: str,
    auth_config: dict | None = None,
    connection_config: dict | None = None,
) -> dict[str, Any]:
    """Return the official MCP SDK connection dict for one server.

    ``endpoint`` is retained as the user-facing legacy field:
    - HTTP/SSE: URL
    - stdio: command

    ``connection_config`` stores transport-specific details such as stdio args,
    env, cwd, or an explicit URL. Bearer auth is translated only for HTTP/SSE
    transports.
    """
    cfg = dict(connection_config or {})
    t = normalize_transport(transport)
    if t not in ALLOWED_TRANSPORTS:
        raise ValueError(
            "transport must be one of: stdio, sse, streamable_http, http"
        )

    if t == "stdio":
        command = str(cfg.get("command") or endpoint or "").strip()
        if not command:
            raise ValueError("stdio MCP requires a command")
        args = cfg.get("args", [])
        if isinstance(args, str):
            args = [args]
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            raise ValueError("stdio MCP args must be a list of strings")
        env = cfg.get("env")
        if env is not None and (
            not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        ):
            raise ValueError("stdio MCP env must be an object of string values")
        out: dict[str, Any] = {
            "transport": "stdio",
            "command": command,
            "args": args,
        }
        if env:
            out["env"] = env
        if cfg.get("cwd"):
            out["cwd"] = str(cfg["cwd"])
        return out

    url = str(cfg.get("url") or endpoint or "").strip()
    if not url:
        raise ValueError(f"{t} MCP requires a URL")
    out = {
        "transport": "sse" if t == "sse" else "streamable_http",
        "url": url,
    }
    headers = dict(cfg.get("headers") or {})
    headers.update(mcp_headers(auth_config))
    if headers:
        out["headers"] = headers
    for key in ("timeout", "sse_read_timeout", "terminate_on_close"):
        if key in cfg:
            out[key] = cfg[key]
    return out


def server_descriptor(row: dict) -> dict[str, Any]:
    """Serialize a DB row into the sandbox MCP job descriptor."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "tool_prefix": row["tool_prefix"],
        "transport": normalize_transport(row["transport"]),
        "endpoint": row["endpoint"],
        "connection": build_connection_config(
            transport=row["transport"],
            endpoint=row["endpoint"],
            auth_config=row.get("auth_config") or {},
            connection_config=row.get("connection_config") or {},
        ),
    }


async def validate_mcp_connection_destination(
    connection: dict[str, Any],
) -> set[str]:
    """Return the validated remote hostname allowlist for one connection.

    stdio commands have no URL to validate and return an empty set.  Remote
    servers must use public HTTPS; callers must invoke this before passing a
    stored descriptor into any host-network sandbox, including legacy rows
    created before this gate existed.
    """
    if connection.get("transport") == "stdio":
        return set()
    target = await validate_public_http_url(
        str(connection.get("url") or ""),
        label="remote MCP endpoint",
        require_https=True,
        trusted_proxy_cidrs=(
            config.sandbox_egress_trusted_proxy_cidrs
            if config.sandbox_egress_mode == "proxy"
            else ()
        ),
    )
    return {target.hostname}
