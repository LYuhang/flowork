"""Reusable tool implementations shared by Agent Runtime adapters.

Concrete tool modules remain available for future adapters. Importing this
package is deliberately SDK-neutral so Codex and Platform MCP do not load an
unrelated Agent SDK merely to validate tool-name collisions.
"""

from __future__ import annotations


_BUILTIN_TOOL_NAMES = frozenset({
    "read_file",
    "write_file",
    "edit_file",
    "grep",
    "read_images",
    "bash",
    "todo",
    "web_search",
})


def builtin_tool_names() -> set[str]:
    """Return reserved names without importing an SDK-specific wrapper."""
    return set(_BUILTIN_TOOL_NAMES)


__all__ = ["builtin_tool_names"]
