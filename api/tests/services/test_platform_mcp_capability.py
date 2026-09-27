from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from vibecanvas_api.services.agent_resources.capability import (
    mint_agent_capability,
    agent_capability_policy,
    verify_agent_capability,
)


_KWARGS = {
    "organization_id": "11111111-1111-1111-1111-111111111111",
    "user_id": "22222222-2222-2222-2222-222222222222",
    "chat_id": "chat-1",
    "turn_id": "turn-1",
    "workspace_scope_id": "workspace-1",
    "runtime_session_id": "runtime-session-1",
    "session_id": "33333333-3333-3333-3333-333333333333",
    "session_generation": 7,
    "membership_id": "44444444-4444-4444-4444-444444444444",
    "authorization_generation": "authz-generation-1",
    "secret": "secret",
    "ttl_s": 60,
    "now": 100,
}


def _resign(payload: dict, secret: str = "secret") -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    digest = hmac.new(
        secret.encode(),
        b"vibecanvas:agent-resource:v1\0" + body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    signature = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return f"{body}.{signature}"


def _payload(token: str) -> dict:
    body = token.split(".", 1)[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_interactive_workflow_preview_ceiling_is_read_only():
    token = mint_agent_capability(server="interactive", **_KWARGS)
    capability = verify_agent_capability(token, secret="secret", server="interactive", now=120)
    assert capability is not None
    assert {"workflow:view", "workflow:use"}.issubset(capability.actions)
    assert not {"workflow:update", "workflow:execute", "workflow:delete"}.intersection(capability.actions)


def test_agent_capability_binds_full_execution_and_policy_scope() -> None:
    token = mint_agent_capability(server="cli", **_KWARGS)

    capability = verify_agent_capability(
        token, secret="secret", server="cli", now=120
    )
    assert capability is not None
    assert capability.audience == "agent-resource"
    assert capability.organization_id == _KWARGS["organization_id"]
    assert capability.chat_id == "chat-1"
    assert capability.turn_id == "turn-1"
    assert capability.workspace_scope_id == "workspace-1"
    assert capability.runtime_session_id == "runtime-session-1"
    assert capability.session_id == _KWARGS["session_id"]
    assert capability.session_generation == 7
    assert capability.membership_id == _KWARGS["membership_id"]
    assert capability.authorization_generation == "authz-generation-1"
    assert capability.approval_mode == "agent"
    expected = agent_capability_policy(
        organization_id=str(_KWARGS["organization_id"]),
        chat_id="chat-1",
        workspace_scope_id="workspace-1",
        server="cli",
    )
    assert capability.resources == expected.resources
    assert capability.actions == expected.actions
    assert verify_agent_capability(
        token, secret="secret", server="browser", now=120
    ) is None
    assert verify_agent_capability(
        token, secret="wrong", server="cli", now=120
    ) is None
    assert verify_agent_capability(
        token, secret="secret", server="cli", now=160
    ) is None


def test_agent_capability_rejects_resigned_scope_widening() -> None:
    token = mint_agent_capability(server="cli", **_KWARGS)
    payload = _payload(token)
    payload["act"].append("workflow:delete")
    widened = _resign(payload)

    assert verify_agent_capability(
        widened,
        secret="secret",
        server="cli",
        now=120,
    ) is None


@pytest.mark.parametrize("server", ["cli", "interactive", "browser"])
def test_resigning_without_changes_is_valid_before_tampering(server):
    token = mint_agent_capability(server=server, **_KWARGS)
    assert verify_agent_capability(_resign(_payload(token)), secret="secret", server=server, now=120) is not None
    for other in {"cli", "interactive", "browser"} - {server}:
        assert verify_agent_capability(token, secret="secret", server=other, now=120) is None


@pytest.mark.parametrize("field,value", [
    ("res", ["organization:another-tenant", "workflow:*"]),
    ("act", ["chat:execute", "agent_cli:call", "workflow:delete"]),
    ("o", "another-tenant"), ("c", "another-chat"),
    ("w", "another-workspace"), ("s", "interactive"),
    ("sg", 0), ("exp", 99), ("v", 2),
])
def test_resigned_identity_and_policy_mismatch_is_rejected(field, value):
    payload = _payload(mint_agent_capability(server="cli", **_KWARGS))
    payload[field] = value
    assert verify_agent_capability(_resign(payload), secret="secret", server="cli", now=120) is None


@pytest.mark.parametrize("server", ["workflow", "build", "config", "task", "deployment", "knowledge", "document", "diagram"])
def test_retired_business_servers_cannot_mint_authority(server):
    with pytest.raises(ValueError, match="unknown Agent server"):
        mint_agent_capability(server=server, **_KWARGS)


def test_cli_authority_does_not_inherit_renderer_or_business_wildcards():
    capability = verify_agent_capability(mint_agent_capability(server="cli", **_KWARGS), secret="secret", now=120)
    assert capability is not None
    assert set(capability.actions) == {"chat:execute", "agent_cli:call"}
    assert all(not resource.endswith(":*") for resource in capability.resources)


def test_legacy_mcp_signature_and_audience_are_not_accepted():
    payload = _payload(mint_agent_capability(server="cli", **_KWARGS))
    payload["aud"] = "platform-mcp"
    assert verify_agent_capability(_resign(payload), secret="secret", now=120) is None
    payload["aud"] = "agent-resource"
    body = _resign(payload).split(".", 1)[0]
    signature = base64.urlsafe_b64encode(hmac.new(b"secret", b"vibecanvas:platform-mcp:v1\0" + body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert verify_agent_capability(f"{body}.{signature}", secret="secret", now=120) is None


def test_agent_capability_rejects_wrong_audience_or_future_issue() -> None:
    token = mint_agent_capability(server="cli", **_KWARGS)
    payload = _payload(token)
    payload["aud"] = "runtime-model"
    assert verify_agent_capability(
        _resign(payload), secret="secret", now=120
    ) is None

    future = mint_agent_capability(
        server="cli",
        **{**_KWARGS, "now": 151},
    )
    assert verify_agent_capability(
        future, secret="secret", now=120
    ) is None


def test_agent_capability_rejects_incomplete_identity() -> None:
    try:
        mint_agent_capability(
            server="cli",
            **{**_KWARGS, "session_id": ""},
        )
    except ValueError as exc:
        assert "identity is incomplete" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty Session identity was accepted")


def test_agent_capability_binds_trusted_approval_mode() -> None:
    token = mint_agent_capability(
        server="cli",
        approval_mode="always_ask",
        **_KWARGS,
    )
    capability = verify_agent_capability(
        token, secret="secret", server="cli", now=120,
    )
    assert capability is not None
    assert capability.approval_mode == "always_ask"

    payload = _payload(token)
    payload["am"] = "untrusted"
    assert verify_agent_capability(
        _resign(payload), secret="secret", server="cli", now=120,
    ) is None
