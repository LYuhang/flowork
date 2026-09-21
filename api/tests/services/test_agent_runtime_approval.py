from vibecanvas_api.services.agent_runtime.approval import (
    PreToolApprovalPolicy,
    is_pre_tool_approval_candidate,
)


def test_official_browser_tabs_read_is_not_an_approval_candidate() -> None:
    assert not is_pre_tool_approval_candidate("browser_tabs", {"action": "list"})
    assert is_pre_tool_approval_candidate("browser_tabs", {"action": "select"})
    assert not is_pre_tool_approval_candidate("browser_tab", {"action": "switch"})


def test_agent_mode_defaults_approval_capable_tool_to_wait() -> None:
    policy = PreToolApprovalPolicy()
    assert policy.evaluate(
        approval_mode="agent",
        source="runtime",
        tool_name="browser_click",
        arguments={"handle": "submit"},
    ).action == "wait"
    assert policy.evaluate(
        approval_mode="agent",
        source="runtime",
        tool_name="browser_click",
        arguments={"handle": "submit", "require_user_auth": False},
    ).action == "allow"


def test_platform_resource_mutations_share_the_same_pre_tool_policy() -> None:
    policy = PreToolApprovalPolicy()
    for tool_name in (
        "task_create_scheduled_run",
        "task_update_scheduled_run",
        "task_delete_scheduled_run",
        "deployment_create",
        "deployment_update",
        "deployment_delete",
    ):
        assert is_pre_tool_approval_candidate(tool_name, {})
        assert policy.evaluate(
            approval_mode="agent",
            source="platform_mcp",
            tool_name=tool_name,
            arguments={},
        ).action == "wait"


def test_turn_modes_override_agent_requested_authorization() -> None:
    policy = PreToolApprovalPolicy()
    assert policy.evaluate(
        approval_mode="always_ask",
        source="platform_mcp",
        tool_name="browser_click",
        arguments={"require_user_auth": False},
    ).action == "wait"
    assert policy.evaluate(
        approval_mode="always_allow",
        source="platform_mcp",
        tool_name="browser_click",
        arguments={"require_user_auth": True},
    ).action == "allow"


def test_codex_native_request_is_host_reviewed_unless_turn_always_allows() -> None:
    policy = PreToolApprovalPolicy()
    assert policy.evaluate(
        approval_mode="agent",
        source="codex_app_server",
        tool_name="shell",
        arguments={"command": "deploy"},
        native_required=True,
    ).action == "wait"
    assert policy.evaluate(
        approval_mode="always_allow",
        source="codex_app_server",
        tool_name="shell",
        arguments={"command": "deploy"},
        native_required=True,
    ).action == "allow"


def test_unknown_runtime_candidate_fails_closed() -> None:
    decision = PreToolApprovalPolicy().evaluate(
        approval_mode="agent",
        source="future_runtime",
        tool_name="unknown_mutation",
        arguments={},
    )
    assert decision.action == "deny"
