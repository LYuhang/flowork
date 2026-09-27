"""Short-lived host-held capabilities for Agent CLI, rendering and browser access.

The scope carried by this token is a ceiling, not an authorization decision.
Concrete resource ids still come from tool arguments and are checked against
the live ``AuthzService`` by the trusted host before a tool implementation is
called.  Keeping the ceiling in the signed token prevents a Runtime from using
a descriptor for a different server or a broader class of operations.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

_DOMAIN = b"vibecanvas:agent-resource:v1\0"
_AUDIENCE = "agent-resource"
_MAX_TOKEN_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class AgentCapabilityPolicy:
    resources: tuple[str, ...]
    actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentCapability:
    organization_id: str
    user_id: str
    chat_id: str
    turn_id: str
    workspace_scope_id: str
    runtime_session_id: str
    session_id: str
    session_generation: int
    membership_id: str
    server: str
    authorization_generation: str
    approval_mode: str
    resources: tuple[str, ...]
    actions: tuple[str, ...]
    issued_at: int
    expires_at: int
    audience: str = _AUDIENCE

    @property
    def tenant_id(self) -> str:
        """Compatibility alias while physical tenant columns mean organization."""
        return self.organization_id

_SERVER_POLICY: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # CLI may request a platform operation, never bypass live resource authorization.
    "cli": (frozenset(), frozenset({"agent_cli:call"})),
    "interactive": (
        frozenset({"interactive_artifact:*", "vfs_path:*", "workflow:*"}),
        frozenset({"platform_mcp:call", "interactive_artifact:create", "vfs_path:view",
                   "vfs_path:update", "workflow:view", "workflow:use"}),
    ),
    "browser": (
        frozenset({"browser_binding:*", "vfs_path:*"}),
        frozenset({"browser_binding:view", "browser_binding:use",
                   "browser_binding:update", "vfs_path:update"}),
    ),
}

def agent_capability_policy(
    *,
    organization_id: str,
    chat_id: str,
    workspace_scope_id: str,
    server: str,
) -> AgentCapabilityPolicy:
    """Return the exact host-owned ceiling for one Agent descriptor."""
    try:
        resource_patterns, action_patterns = _SERVER_POLICY[server]
    except KeyError as exc:
        raise ValueError(f"unknown Agent server: {server}") from exc
    required = {
        "organization_id": organization_id,
        "chat_id": chat_id,
        "workspace_scope_id": workspace_scope_id,
    }
    if any(not str(value).strip() for value in required.values()):
        raise ValueError("Agent policy identity is incomplete")
    resources = {
        f"organization:{organization_id}",
        f"chat:{chat_id}",
        f"chat_workspace:{workspace_scope_id}",
        f"agent_capability:{server}",
        *resource_patterns,
    }
    actions = {"chat:execute", *action_patterns}
    return AgentCapabilityPolicy(
        resources=tuple(sorted(resources)),
        actions=tuple(sorted(actions)),
    )


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(body: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), _DOMAIN + body.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64url(digest)


def mint_agent_capability(
    *,
    organization_id: str,
    user_id: str,
    chat_id: str,
    turn_id: str,
    workspace_scope_id: str,
    runtime_session_id: str,
    session_id: str,
    session_generation: int,
    membership_id: str,
    server: str,
    authorization_generation: str,
    secret: str,
    ttl_s: int,
    approval_mode: str = "agent",
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    if approval_mode not in {"agent", "always_ask", "always_allow"}:
        raise ValueError("invalid Agent approval mode")
    if any(
        not str(value).strip()
        for value in (
            organization_id,
            user_id,
            chat_id,
            turn_id,
            workspace_scope_id,
            runtime_session_id,
            session_id,
            membership_id,
            authorization_generation,
        )
    ) or int(session_generation) <= 0:
        raise ValueError("Agent capability identity is incomplete")
    policy = agent_capability_policy(
        organization_id=organization_id,
        chat_id=chat_id,
        workspace_scope_id=workspace_scope_id,
        server=server,
    )
    payload = {
        "v": 1,
        "aud": _AUDIENCE,
        "o": organization_id,
        "u": user_id,
        "c": chat_id,
        "t": turn_id,
        "w": workspace_scope_id,
        "rs": runtime_session_id,
        "sid": session_id,
        "sg": int(session_generation),
        "mid": membership_id,
        "s": server,
        "ag": authorization_generation,
        "am": approval_mode,
        "res": list(policy.resources),
        "act": list(policy.actions),
        "iat": issued_at,
        "exp": issued_at + max(1, int(ttl_s)),
    }
    body = _b64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    token = f"{body}.{_signature(body, secret)}"
    if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:  # pragma: no cover
        raise ValueError("Agent capability is too large")
    return token


def verify_agent_capability(
    token: str,
    *,
    secret: str,
    server: str | None = None,
    now: int | None = None,
) -> AgentCapability | None:
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
        capability = AgentCapability(
            organization_id=str(payload["o"]),
            user_id=str(payload["u"]),
            chat_id=str(payload["c"]),
            turn_id=str(payload["t"]),
            workspace_scope_id=str(payload["w"]),
            runtime_session_id=str(payload["rs"]),
            session_id=str(payload["sid"]),
            session_generation=int(payload["sg"]),
            membership_id=str(payload["mid"]),
            server=str(payload["s"]),
            authorization_generation=str(payload["ag"]),
            approval_mode=str(payload.get("am") or "agent"),
            resources=tuple(str(item) for item in payload["res"]),
            actions=tuple(str(item) for item in payload["act"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
        )
        expected_policy = agent_capability_policy(
            organization_id=capability.organization_id,
            chat_id=capability.chat_id,
            workspace_scope_id=capability.workspace_scope_id,
            server=capability.server,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        return None
    if server is not None and capability.server != server:
        return None
    if capability.issued_at > current + 30 or capability.expires_at <= current:
        return None
    if capability.expires_at <= capability.issued_at:
        return None
    if capability.session_generation <= 0:
        return None
    if capability.approval_mode not in {"agent", "always_ask", "always_allow"}:
        return None
    if capability.resources != expected_policy.resources:
        return None
    if capability.actions != expected_policy.actions:
        return None
    return capability


__all__ = [
    "AgentCapability",
    "AgentCapabilityPolicy",
    "mint_agent_capability",
    "agent_capability_policy",
    "verify_agent_capability",
]
