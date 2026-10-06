"""Short-lived capabilities for host-brokered Workflow model calls."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import time


_DOMAIN = b"vibecanvas:runtime-workflow-model:v1\0"
_AUDIENCE = "runtime-workflow-model"
_MAX_TOKEN_BYTES = 16 * 1024
_EXECUTION_RESOURCE_TYPES = frozenset({
    "workflow",  # Local CLI execution: authority follows resource access, not a Chat turn.
    "agent_run",
    "deployment_invocation",
    "task",
    "task_execution",
    "workflow_execution",
})


@dataclass(frozen=True, slots=True)
class RuntimeWorkflowModelCapability:
    organization_id: str
    user_id: str
    workflow_id: str
    execution_id: str
    execution_resource_type: str
    credential_id: str | None
    provider: str
    model: str
    config_revision: str
    authorization_generation: str
    issued_at: int
    expires_at: int
    principal_type: str = "user"
    principal_id: str = ""
    principal_generation: int = 0
    audience: str = _AUDIENCE


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signature(body: str, secret: str) -> str:
    return _b64url(
        hmac.new(
            secret.encode(),
            _DOMAIN + body.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


def mint_runtime_workflow_model_capability(
    *,
    organization_id: str,
    user_id: str,
    workflow_id: str,
    execution_id: str,
    execution_resource_type: str,
    credential_id: str | None,
    provider: str,
    model: str,
    config_revision: str,
    authorization_generation: str,
    secret: str,
    ttl_s: int,
    now: int | None = None,
    principal_type: str = "user",
    principal_id: str | None = None,
    principal_generation: int = 0,
) -> str:
    if execution_resource_type not in _EXECUTION_RESOURCE_TYPES:
        raise ValueError("unsupported Workflow model execution resource type")
    issued_at = int(time.time()) if now is None else int(now)
    principal_id = principal_id or user_id
    if principal_type not in {"user", "service_account"}:
        raise ValueError("unsupported Workflow execution principal type")
    if principal_type == "service_account" and principal_generation <= 0:
        raise ValueError("service account generation must be positive")
    if execution_resource_type == "workflow" and (
        execution_id != workflow_id or principal_type != "user" or principal_id != user_id
    ):
        raise ValueError("local Workflow capability must bind the same Workflow and user")
    resources = [
        f"workflow:{workflow_id}",
        f"{execution_resource_type}:{execution_id}",
    ]
    actions = [
        "model:invoke",
        "workflow:execute",
        f"{execution_resource_type}:execute",
    ]
    if credential_id is not None:
        resources.append(f"llm_credential:{credential_id}")
        actions.append("llm_credential:use")
    if principal_type == "service_account":
        resources.append(f"service_account:{principal_id}")
        actions.append("service_account:act_as")
    payload = {
        "v": 1,
        "aud": _AUDIENCE,
        "o": organization_id,
        "u": user_id,
        "pt": principal_type,
        "pid": principal_id,
        "pg": int(principal_generation),
        "w": workflow_id,
        "e": execution_id,
        "ert": execution_resource_type,
        "cred": credential_id,
        "provider": provider,
        "model": model,
        "cr": config_revision,
        "ag": authorization_generation,
        "res": sorted(resources),
        "act": sorted(actions),
        "iat": issued_at,
        "exp": issued_at + max(1, int(ttl_s)),
    }
    body = _b64url(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )
    token = f"{body}.{_signature(body, secret)}"
    if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:  # pragma: no cover
        raise ValueError("runtime Workflow model capability is too large")
    return token


def verify_runtime_workflow_model_capability(
    token: str,
    *,
    secret: str,
    now: int | None = None,
) -> RuntimeWorkflowModelCapability | None:
    current = int(time.time()) if now is None else int(now)
    if not token or len(token.encode("utf-8", errors="ignore")) > _MAX_TOKEN_BYTES:
        return None
    try:
        body, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, _signature(body, secret)):
            return None
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
        if payload.get("v") != 1 or payload.get("aud") != _AUDIENCE:
            return None
        credential_id = (
            str(payload["cred"]) if payload.get("cred") is not None else None
        )
        execution_resource_type = str(payload["ert"])
        principal_type = str(payload.get("pt") or "user")
        principal_id = str(payload.get("pid") or payload["u"])
        principal_generation = int(payload.get("pg") or 0)
        if principal_type not in {"user", "service_account"}:
            return None
        if execution_resource_type not in _EXECUTION_RESOURCE_TYPES:
            return None
        expected_resources = {
            f"workflow:{payload['w']}",
            f"{execution_resource_type}:{payload['e']}",
        }
        expected_actions = {
            "workflow:execute",
            "model:invoke",
            f"{execution_resource_type}:execute",
        }
        if credential_id is not None:
            expected_resources.add(f"llm_credential:{credential_id}")
            expected_actions.add("llm_credential:use")
        if principal_type == "service_account":
            expected_resources.add(f"service_account:{principal_id}")
            expected_actions.add("service_account:act_as")
        if (
            {str(item) for item in payload["res"]} != expected_resources
            or {str(item) for item in payload["act"]} != expected_actions
        ):
            return None
        capability = RuntimeWorkflowModelCapability(
            organization_id=str(payload["o"]),
            user_id=str(payload["u"]),
            workflow_id=str(payload["w"]),
            execution_id=str(payload["e"]),
            execution_resource_type=execution_resource_type,
            credential_id=credential_id,
            provider=str(payload["provider"]),
            model=str(payload["model"]),
            config_revision=str(payload["cr"]),
            authorization_generation=str(payload["ag"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            principal_type=principal_type,
            principal_id=principal_id,
            principal_generation=principal_generation,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        base64.binascii.Error,
    ):
        return None
    if (
        not capability.organization_id
        or not capability.user_id
        or not capability.workflow_id
        or not capability.execution_id
        or not capability.principal_id
        or (capability.execution_resource_type == "workflow" and (
            capability.execution_id != capability.workflow_id
            or capability.principal_type != "user"
            or capability.principal_id != capability.user_id
        ))
        or not capability.provider
        or not capability.model
        or capability.issued_at > current + 30
        or capability.expires_at <= current
        or capability.expires_at <= capability.issued_at
        or (
            capability.principal_type == "service_account"
            and capability.principal_generation <= 0
        )
    ):
        return None
    return capability


__all__ = [
    "RuntimeWorkflowModelCapability",
    "mint_runtime_workflow_model_capability",
    "verify_runtime_workflow_model_capability",
]
