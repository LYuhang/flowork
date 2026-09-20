"""Short-lived, least-privilege capabilities for host-brokered model calls.

The token is deliberately useful only as an API key *to the Flowork model
broker*.  It never contains the provider credential or a credential-bearing
URL.  Every request is re-authorized by the host before a secret is resolved.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Iterable


_DOMAIN = b"vibecanvas:runtime-model:v1\0"
_AUDIENCE = "runtime-model"
_MAX_TOKEN_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeModelCapability:
    organization_id: str
    user_id: str
    chat_id: str
    turn_id: str
    runtime_session_id: str
    session_id: str
    session_generation: int
    membership_id: str
    credential_id: str | None
    provider: str
    model: str
    config_revision: str
    authorization_generation: str
    resources: tuple[str, ...]
    actions: tuple[str, ...]
    issued_at: int
    expires_at: int
    managed_profile_id: str | None = None
    audience: str = _AUDIENCE


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(body: str, secret: str) -> str:
    return _b64url(
        hmac.new(
            secret.encode("utf-8"),
            _DOMAIN + body.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


def model_config_revision(*, provider: str, model: str, updated_at: object) -> str:
    """Fingerprint non-secret model metadata used to fence stale leases."""
    raw = json.dumps(
        {
            "provider": str(provider or "").strip().lower(),
            "model": str(model or "").strip(),
            "updated_at": str(updated_at or "platform-process-config"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def authorization_model_generation(*, model_id: str) -> str:
    """Bind a lease to the pinned OpenFGA policy generation.

    Relationship changes are checked live for every broker request.  This
    generation additionally makes a policy-model rollout invalidate old
    capabilities without placing an OpenFGA credential in the token.
    """
    raw = f"openfga\0{model_id.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def mint_runtime_model_capability(
    *,
    organization_id: str,
    user_id: str,
    chat_id: str,
    turn_id: str,
    runtime_session_id: str,
    session_id: str,
    session_generation: int,
    membership_id: str,
    credential_id: str | None,
    managed_profile_id: str | None = None,
    provider: str,
    model: str,
    config_revision: str,
    authorization_generation: str,
    resources: Iterable[str],
    actions: Iterable[str],
    secret: str,
    ttl_s: int,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "v": 1,
        "aud": _AUDIENCE,
        "o": organization_id,
        "u": user_id,
        "c": chat_id,
        "t": turn_id,
        "rs": runtime_session_id,
        "sid": session_id,
        "sg": int(session_generation),
        "mid": membership_id,
        "cred": credential_id,
        "mp": managed_profile_id,
        "p": provider,
        "m": model,
        "cr": config_revision,
        "ag": authorization_generation,
        "res": sorted(set(resources)),
        "act": sorted(set(actions)),
        "iat": issued_at,
        "exp": issued_at + max(1, int(ttl_s)),
    }
    body = _b64url(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    token = f"{body}.{_signature(body, secret)}"
    if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:  # pragma: no cover
        raise ValueError("runtime model capability is too large")
    return token


def verify_runtime_model_capability(
    token: str,
    *,
    secret: str,
    now: int | None = None,
) -> RuntimeModelCapability | None:
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
        resources = tuple(str(item) for item in payload["res"])
        actions = tuple(str(item) for item in payload["act"])
        capability = RuntimeModelCapability(
            organization_id=str(payload["o"]),
            user_id=str(payload["u"]),
            chat_id=str(payload["c"]),
            turn_id=str(payload["t"]),
            runtime_session_id=str(payload["rs"]),
            session_id=str(payload["sid"]),
            session_generation=int(payload["sg"]),
            membership_id=str(payload["mid"]),
            credential_id=(
                str(payload["cred"]) if payload.get("cred") is not None else None
            ),
            managed_profile_id=(
                str(payload["mp"]) if payload.get("mp") is not None else None
            ),
            provider=str(payload["p"]),
            model=str(payload["m"]),
            config_revision=str(payload["cr"]),
            authorization_generation=str(payload["ag"]),
            resources=resources,
            actions=actions,
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
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
    if capability.issued_at > current + 30 or capability.expires_at <= current:
        return None
    if capability.expires_at <= capability.issued_at:
        return None
    required_resources = {f"chat:{capability.chat_id}"}
    required_actions = {"chat:execute", "model:invoke"}
    if capability.credential_id:
        required_resources.add(f"llm_credential:{capability.credential_id}")
        required_actions.add("llm_credential:use")
    if capability.credential_id and capability.managed_profile_id:
        return None
    if not required_resources.issubset(capability.resources):
        return None
    if not required_actions.issubset(capability.actions):
        return None
    return capability


__all__ = [
    "RuntimeModelCapability",
    "authorization_model_generation",
    "mint_runtime_model_capability",
    "model_config_revision",
    "verify_runtime_model_capability",
]
