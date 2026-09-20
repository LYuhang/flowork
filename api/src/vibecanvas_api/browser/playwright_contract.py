"""Model-facing contract for the official Playwright MCP integration.

The upstream server intentionally exposes a broad automation and debugging
surface.  Flowork connects that server to a user's already-authenticated real
browser, so only the product-reviewed subset below may cross the Runtime
boundary.  Both Runtime adapters import this module; filtering in only one
adapter would make ``/browser`` behave differently between LangChain and Codex.

Session leases, tenant scoping and the remote CDP relay are Flowork control
plane concerns.  They are deliberately not represented as a second set of
Agent-callable browser tools.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar


# Audited against the default tool catalog returned by @playwright/mcp@0.0.79.
# Keep this exact versioned subset in sync with api/playwright-runtime's lockfile.
PLAYWRIGHT_AGENT_TOOLS: tuple[str, ...] = (
    "browser_click",
    "browser_close",
    "browser_console_messages",
    "browser_drag",
    "browser_drop",
    "browser_file_upload",
    "browser_find",
    "browser_fill_form",
    "browser_handle_dialog",
    "browser_hover",
    "browser_navigate",
    "browser_navigate_back",
    "browser_network_request",
    "browser_network_requests",
    "browser_press_key",
    "browser_resize",
    "browser_select_option",
    "browser_snapshot",
    "browser_tabs",
    "browser_take_screenshot",
    "browser_type",
    "browser_wait_for",
)

PLAYWRIGHT_AGENT_TOOL_SET = frozenset(PLAYWRIGHT_AGENT_TOOLS)

# These tools are especially important to name in regression tests.  They are
# exported by upstream's default ``core`` capability but are incompatible with
# Flowork's no-remote-code boundary.  Cookie/storage mutation and network route
# interception are excluded by the allow-list as well.
PLAYWRIGHT_FORBIDDEN_TOOLS = frozenset({
    "browser_evaluate",
    "browser_run_code_unsafe",
})

PLAYWRIGHT_AUDITED_UPSTREAM_TOOLS = frozenset({
    *PLAYWRIGHT_AGENT_TOOLS,
    *PLAYWRIGHT_FORBIDDEN_TOOLS,
})


_Tool = TypeVar("_Tool")


def filter_playwright_tools(tools: Iterable[_Tool]) -> list[_Tool]:
    """Return only reviewed official tools, preserving upstream order."""

    return [
        tool
        for tool in tools
        if str(getattr(tool, "name", "") or "") in PLAYWRIGHT_AGENT_TOOL_SET
    ]


def playwright_tool_is_allowed(name: str) -> bool:
    """Fail-closed call-time check for a Playwright MCP tool name."""

    return str(name or "") in PLAYWRIGHT_AGENT_TOOL_SET


__all__ = [
    "PLAYWRIGHT_AGENT_TOOLS",
    "PLAYWRIGHT_AGENT_TOOL_SET",
    "PLAYWRIGHT_AUDITED_UPSTREAM_TOOLS",
    "PLAYWRIGHT_FORBIDDEN_TOOLS",
    "filter_playwright_tools",
    "playwright_tool_is_allowed",
]
