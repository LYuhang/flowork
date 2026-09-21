"""Runtime-neutral pre-tool approval policy.

Runtimes report a fully materialized tool call before invoking its handler.
The host evaluates the per-Turn policy and either replies immediately or
persists a HITL gate. No SDK-specific object is accepted by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


PRE_TOOL_APPROVAL_TOOLS: frozenset[str] = frozenset({
    "browser_navigate",
    "browser_navigate_back",
    "browser_close",
    "browser_resize",
    "browser_tabs",
    "browser_click",
    "browser_drag",
    "browser_hover",
    "browser_type",
    "browser_fill_form",
    "browser_file_upload",
    "browser_handle_dialog",
    "browser_drop",
    "browser_select_option",
    "browser_press_key",
    "task_create_scheduled_run",
    "task_update_scheduled_run",
    "task_delete_scheduled_run",
    "task_cancel",
    "task_resume",
    "deployment_create",
    "deployment_update",
    "deployment_delete",
})


def is_pre_tool_approval_candidate(
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    """Return whether a tool call belongs to the platform approval surface."""
    if tool_name not in PRE_TOOL_APPROVAL_TOOLS:
        return False
    if tool_name == "browser_tabs":
        # Listing is read-only. New/select/close changes the visible controlled
        # browser state and therefore remains part of the approval surface.
        return str(arguments.get("action") or "") in {"new", "select", "close"}
    return True


@dataclass(frozen=True)
class ApprovalPolicyDecision:
    action: Literal["allow", "wait", "deny"]
    reason: str


class PreToolApprovalPolicy:
    """Evaluate one Runtime-neutral approval candidate on the host."""

    def evaluate(
        self,
        *,
        approval_mode: str,
        source: str,
        tool_name: str,
        arguments: dict[str, Any],
        native_required: bool = False,
    ) -> ApprovalPolicyDecision:
        if approval_mode == "always_allow":
            return ApprovalPolicyDecision("allow", "turn_policy_always_allow")
        if native_required:
            # A Runtime-native request means the SDK has already stopped at a
            # pre-execution approval point. The host remains the only owner of
            # the user decision and durable UI state.
            return ApprovalPolicyDecision("wait", f"{source}_native_request")
        if not is_pre_tool_approval_candidate(tool_name, arguments):
            # Fail closed: an arbitrary tool cannot opt itself into a policy
            # class the host does not recognize.
            return ApprovalPolicyDecision("deny", "unknown_approval_candidate")
        if approval_mode == "always_ask":
            return ApprovalPolicyDecision("wait", "turn_policy_always_ask")
        if approval_mode == "agent":
            # Approval-capable tool parameters default to requiring a user.
            if bool(arguments.get("require_user_auth", True)):
                return ApprovalPolicyDecision("wait", "tool_requested_user_auth")
            return ApprovalPolicyDecision("allow", "tool_marked_low_risk")
        return ApprovalPolicyDecision("deny", "invalid_approval_mode")


def requires_user_approval(
    tool_name: str,
    arguments: dict[str, Any],
    approval_mode: str,
    *,
    source: str = "runtime",
) -> bool:
    """Return whether a recognized tool call must wait for user approval."""
    return PreToolApprovalPolicy().evaluate(
        approval_mode=approval_mode,
        source=source,
        tool_name=tool_name,
        arguments=arguments,
    ).action == "wait"
