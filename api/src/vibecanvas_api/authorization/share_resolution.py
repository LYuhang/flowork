"""Short-lived capability returned by explicit share-target resolution."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from vibecanvas_api.config import config

from .types import (
    RelationshipBinding,
    RelationshipSubject,
    RelationshipSubjectType,
    ResourceRef,
)


_AUDIENCE = "flowork:share-resolution:v1"
_TTL_SECONDS = 300


def _b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(body: str) -> str:
    return _b64u(
        hmac.new(
            config.signing_secret.encode("utf-8"),
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


@dataclass(frozen=True, slots=True)
class ShareResolution:
    actor_user_id: str
    session_id: str
    owner_organization_id: str
    resource_type: str
    resource_id: str
    subject_type: str
    subject_id: str
    subject_relation: str | None
    allowed_relations: tuple[str, ...]


def mint_share_resolution(value: ShareResolution) -> str:
    payload = {
        "aud": _AUDIENCE,
        "exp": int(time.time()) + _TTL_SECONDS,
        "actor": value.actor_user_id,
        "session": value.session_id,
        "owner_org": value.owner_organization_id,
        "resource_type": value.resource_type,
        "resource_id": value.resource_id,
        "subject_type": value.subject_type,
        "subject_id": value.subject_id,
        "subject_relation": value.subject_relation,
        "allowed_relations": list(value.allowed_relations),
    }
    body = _b64u(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    return f"{body}.{_signature(body)}"


def verify_share_resolution(
    token: str,
    *,
    actor_user_id: str,
    session_id: str,
    owner_organization_id: str,
    resource_type: str,
    resource_id: str,
) -> ShareResolution:
    try:
        body, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, _signature(body)):
            raise ValueError("invalid_share_resolution")
        payload = json.loads(_decode(body))
        allowed = tuple(str(item) for item in payload["allowed_relations"])
        result = ShareResolution(
            actor_user_id=str(payload["actor"]),
            session_id=str(payload["session"]),
            owner_organization_id=str(payload["owner_org"]),
            resource_type=str(payload["resource_type"]),
            resource_id=str(payload["resource_id"]),
            subject_type=str(payload["subject_type"]),
            subject_id=str(payload["subject_id"]),
            subject_relation=(
                str(payload["subject_relation"])
                if payload.get("subject_relation") is not None
                else None
            ),
            allowed_relations=allowed,
        )
        if payload.get("aud") != _AUDIENCE or int(payload["exp"]) < int(time.time()):
            raise ValueError("invalid_share_resolution")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_share_resolution") from exc
    if (
        result.actor_user_id != actor_user_id
        or result.session_id != session_id
        or result.owner_organization_id != owner_organization_id
        or result.resource_type != resource_type
        or result.resource_id != resource_id
    ):
        raise ValueError("invalid_share_resolution")
    return result


def binding_from_share_resolution(
    token: str,
    *,
    relation: str,
    actor_user_id: str,
    session_id: str,
    resource: ResourceRef,
) -> RelationshipBinding:
    resolution = verify_share_resolution(
        token,
        actor_user_id=actor_user_id,
        session_id=session_id,
        owner_organization_id=resource.organization_id,
        resource_type=resource.type.value,
        resource_id=resource.id,
    )
    if relation not in resolution.allowed_relations:
        raise ValueError("share_relation_not_resolved")
    try:
        subject_type = RelationshipSubjectType(resolution.subject_type)
    except ValueError as exc:
        raise ValueError("invalid_share_resolution") from exc
    return RelationshipBinding(
        subject=RelationshipSubject(
            type=subject_type,
            id=resolution.subject_id,
            relation=resolution.subject_relation,
        ),
        relation=relation,
        resource=resource,
    )
