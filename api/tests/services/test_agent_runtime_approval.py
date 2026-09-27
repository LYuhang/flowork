"""Only explicit Runtime-native approval gates use the host policy."""

import pytest

from vibecanvas_api.services.agent_runtime.approval import PreToolApprovalPolicy


@pytest.mark.parametrize("mode, action", [("agent", "wait"), ("always_ask", "wait"), ("always_allow", "allow")])
def test_native_gate_obeys_turn_approval_mode(mode, action):
    result = PreToolApprovalPolicy().evaluate(
        approval_mode=mode, source="codex_app_server", native_required=True,
    )
    assert result.action == action
    assert result.reason == ("turn_policy_always_allow" if action == "allow" else "codex_app_server_native_request")


@pytest.mark.parametrize("mode", ["agent", "always_ask", "always_allow"])
@pytest.mark.parametrize("source", ["platform_mcp", "custom_mcp", "future_runtime"])
def test_non_native_candidates_fail_closed_in_every_mode(mode, source):
    result = PreToolApprovalPolicy().evaluate(approval_mode=mode, source=source)
    assert result.action == "deny"
    assert result.reason == "unsupported_approval_request"


@pytest.mark.parametrize("mode", ["", "unknown", "ALWAYS_ALLOW"])
@pytest.mark.parametrize("native_required", [True, False])
def test_invalid_approval_mode_fails_closed(mode, native_required):
    result = PreToolApprovalPolicy().evaluate(
        approval_mode=mode, source="codex_app_server", native_required=native_required,
    )
    assert result.action == "deny"
    assert result.reason == "invalid_approval_mode"
