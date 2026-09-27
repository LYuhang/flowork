"""Catalog of built-in render MCP tools; business resources use flowork-cli."""

from __future__ import annotations

import hashlib
import json
from typing import Final

PLATFORM_MCP_METADATA: Final[dict[str, dict[str, object]]] = {
    "interactive": {
        "name": "Interactive",
        "description": "Display files, URLs, or pinned workflow versions with render_preview. Ask the user to select options with render_choices, which waits inside the tool call for confirmation or cancellation.",
        "activation": "Always available",
        "activation_mode": "base",
        "runtime_types": ["codex"],
    },
}

BUILTIN_MCP_METADATA: Final[dict[str, dict[str, object]]] = PLATFORM_MCP_METADATA
BUILTIN_MCP_CONTRACT_REVISION: Final[str] = "sha256:" + hashlib.sha256(
    json.dumps(BUILTIN_MCP_METADATA, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def builtin_mcp_description(server: str) -> str:
    """Return an active built-in MCP description, never a retired empty server."""
    try:
        return str(BUILTIN_MCP_METADATA[server]["description"])
    except KeyError as exc:
        raise ValueError(f"unknown built-in MCP capability: {server}") from exc
