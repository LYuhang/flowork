"""Signed Turn authority for private sandbox-Hub to Host Gateway calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


_AUDIENCE = "sandbox-mcp-host-gateway"
_DOMAIN = b"flowork:sandbox-mcp-execution:v1\0"
_MAX_TOKEN_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class McpExecutionCapability:
    organization_id: str
    user_id: str
    chat_id: str
    runtime_session_id: str
    turn_id: str
    sandbox_id: str
    sandbox_generation: int
    selected_mcp_revision: int
    active_platform_capabilities: tuple[str, ...]
    authorization_generation: str
    issued_at: int
    expires_at: int


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(body: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        _DOMAIN + body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _encode(digest)


def mint_mcp_execution_capability(
    *,
    organization_id: str,
    user_id: str,
    chat_id: str,
    runtime_session_id: str,
    turn_id: str,
    sandbox_id: str,
    sandbox_generation: int,
    selected_mcp_revision: int,
    active_platform_capabilities: list[str],
    authorization_generation: str,
    secret: str,
    ttl_s: int,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    required = (
        organization_id,
        user_id,
        chat_id,
        runtime_session_id,
        turn_id,
        sandbox_id,
        authorization_generation,
    )
    if any(not str(item).strip() for item in required):
        raise ValueError("MCP execution capability identity is incomplete")
    if sandbox_generation <= 0 or selected_mcp_revision < 0:
        raise ValueError("MCP execution capability revision is invalid")
    capabilities = sorted(set(active_platform_capabilities))
    if len(capabilities) != len(active_platform_capabilities):
        raise ValueError("MCP execution capabilities must be unique")
    payload = {
        "v": 1,
        "aud": _AUDIENCE,
        "o": organization_id,
        "u": user_id,
        "c": chat_id,
        "rs": runtime_session_id,
        "t": turn_id,
        "sb": sandbox_id,
        "sg": sandbox_generation,
        "mr": selected_mcp_revision,
        "pc": capabilities,
        "ag": authorization_generation,
        "iat": issued_at,
        "exp": issued_at + max(1, int(ttl_s)),
    }
    body = _encode(json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode())
    token = f"{body}.{_signature(body, secret)}"
    if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:
        raise ValueError("MCP execution capability is too large")
    return token


def verify_mcp_execution_capability(
    token: str,
    *,
    secret: str,
    now: int | None = None,
) -> McpExecutionCapability | None:
    current = int(time.time()) if now is None else int(now)
    if not token or len(token.encode("utf-8", errors="ignore")) > _MAX_TOKEN_BYTES:
        return None
    try:
        body, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, _signature(body, secret)):
            return None
        payload = json.loads(_decode(body))
        if payload.get("v") != 1 or payload.get("aud") != _AUDIENCE:
            return None
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        if issued_at > current + 30 or expires_at <= current:
            return None
        capabilities = tuple(str(item) for item in payload["pc"])
        if tuple(sorted(set(capabilities))) != capabilities:
            return None
        return McpExecutionCapability(
            organization_id=str(payload["o"]),
            user_id=str(payload["u"]),
            chat_id=str(payload["c"]),
            runtime_session_id=str(payload["rs"]),
            turn_id=str(payload["t"]),
            sandbox_id=str(payload["sb"]),
            sandbox_generation=int(payload["sg"]),
            selected_mcp_revision=int(payload["mr"]),
            active_platform_capabilities=capabilities,
            authorization_generation=str(payload["ag"]),
            issued_at=issued_at,
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


__all__ = [
    "McpExecutionCapability",
    "mint_mcp_execution_capability",
    "verify_mcp_execution_capability",
]
