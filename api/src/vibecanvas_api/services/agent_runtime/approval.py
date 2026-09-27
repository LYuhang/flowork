"""Host policy for Runtime-native, pre-execution approval requests.

CLI resource mutations and browser file transfers own their durable approval
leases. They do not route through an MCP tool-name allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ApprovalPolicyDecision:
    action: Literal["allow", "wait", "deny"]
    reason: str


class PreToolApprovalPolicy:
    """Evaluate an explicit native approval gate without trusting tool arguments."""

    def evaluate(
        self,
        *,
        approval_mode: str,
        source: str,
        native_required: bool = False,
    ) -> ApprovalPolicyDecision:
        if approval_mode not in {"agent", "always_ask", "always_allow"}:
            return ApprovalPolicyDecision("deny", "invalid_approval_mode")
        if not native_required:
            return ApprovalPolicyDecision("deny", "unsupported_approval_request")
        if approval_mode == "always_allow":
            return ApprovalPolicyDecision("allow", "turn_policy_always_allow")
        # The SDK has already paused before executing. Only the host owns the
        # durable user decision; model-provided arguments cannot bypass it.
        return ApprovalPolicyDecision("wait", f"{source}_native_request")
