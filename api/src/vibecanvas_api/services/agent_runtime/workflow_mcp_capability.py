"""Audience-separated, execution-scoped capabilities for selected MCP tools."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import re
import time
from uuid import UUID

_DOMAIN = b"vibecanvas:runtime-workflow-mcp:v1\0"
_AUDIENCE = "runtime-workflow-mcp"
_EXECUTIONS = {"workflow", "agent_run", "workflow_execution", "task", "task_execution", "deployment_invocation"}


@dataclass(frozen=True, slots=True)
class WorkflowMcpCapability:
    organization_id: str
    user_id: str
    workflow_id: str
    execution_id: str
    execution_resource_type: str
    principal_type: str
    principal_id: str
    principal_generation: int
    authorization_generation: str
    server_id: str
    tools_fingerprint: str
    issued_at: int
    expires_at: int
    audience: str = _AUDIENCE


def _valid(capability: WorkflowMcpCapability, now: int) -> bool:
    for identifier in (capability.organization_id, capability.user_id, capability.principal_id, capability.server_id):
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            return False
    return bool(
        capability.audience == _AUDIENCE
        and capability.execution_resource_type in _EXECUTIONS
        and (capability.execution_resource_type != "workflow" or (
            capability.execution_id == capability.workflow_id and capability.principal_type == "user"
        ))
        and isinstance(capability.workflow_id, str) and capability.workflow_id
        and isinstance(capability.execution_id, str) and capability.execution_id
        and isinstance(capability.authorization_generation, str) and capability.authorization_generation
        and isinstance(capability.tools_fingerprint, str)
        and re.fullmatch(r"[a-f0-9]{64}", capability.tools_fingerprint)
        and type(capability.issued_at) is int and type(capability.expires_at) is int
        and capability.issued_at <= now + 30
        and capability.expires_at > max(now, capability.issued_at)
        and type(capability.principal_generation) is int
        and ((capability.principal_type == "user" and capability.principal_id == capability.user_id
              and capability.principal_generation == 0)
             or (capability.principal_type == "service_account" and capability.principal_generation > 0))
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signature(body: str, secret: str) -> str:
    return _encode(hmac.new(secret.encode(), _DOMAIN + body.encode("ascii"), hashlib.sha256).digest())


def mint_workflow_mcp_capability(*, secret: str, ttl_s: int, now: int | None = None, **claims) -> str:
    issued = int(time.time()) if now is None else now
    capability = WorkflowMcpCapability(**claims, issued_at=issued, expires_at=issued + max(1, ttl_s))
    if not _valid(capability, issued):
        raise ValueError("Invalid workflow MCP capability claims")
    body = _encode(json.dumps(asdict(capability), sort_keys=True, separators=(",", ":")).encode())
    token = body + "." + _signature(body, secret)
    if len(token) > 16384:
        raise ValueError("Workflow MCP capability too large")
    return token


def verify_workflow_mcp_capability(token: str, *, secret: str, server_id: str,
                                   now: int | None = None) -> WorkflowMcpCapability | None:
    if not isinstance(token, str) or not token or len(token) > 16384:
        return None
    try:
        body, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, _signature(body, secret)):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        capability = WorkflowMcpCapability(**payload)
        current = int(time.time()) if now is None else now
        if capability.server_id != server_id or not _valid(capability, current):
            return None
        return capability
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
