"""Canonical metadata for built-in sandbox MCP capabilities.

This module feeds Runtime projection, command activation, and internal contract
tests. Platform capabilities are implementation details and are not exposed as
user-managed MCP resources.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

from vibecanvas_api.agents.prompts.diagram import DIAGRAM_MCP_TOOL_NAMES
from vibecanvas_api.agents.prompts.document import DOCUMENT_MCP_TOOL_NAMES


PLATFORM_MCP_METADATA: Final[dict[str, dict[str, object]]] = {
    "config": {
        "name": "Configuration",
        "description": "Read safe platform, model, current Chat, and workflow configuration.",
        "activation": "Always available",
        "activation_mode": "base",
        "runtime_types": ["codex"],
    },
    "interactive": {
        "name": "Interactive",
        "description": "Render durable rich content and optionally pause for an explicit Continue action.",
        "activation": "Always available",
        "activation_mode": "base",
        "runtime_types": ["codex"],
    },
    "workflow": {
        "name": "Workflow",
        "description": "List and inspect workflows that the current user is authorized to access.",
        "activation": "/workflow",
        "activation_mode": "command",
        "runtime_types": ["codex"],
    },
    "task": {
        "name": "Task",
        "description": "Inspect and manage Task Center work, scheduled runs, cancellation, and resume.",
        "activation": "/task",
        "activation_mode": "command",
        "runtime_types": ["codex"],
    },
    "deployment": {
        "name": "Deployment",
        "description": "List, inspect, create, update, and remove authorized workflow deployments.",
        "activation": "/deployment",
        "activation_mode": "command",
        "runtime_types": ["codex"],
    },
    "knowledge": {
        "name": "Knowledge",
        "description": "Materialize, create, version, update, and progressively search authorized Knowledge packages.",
        "activation": "/knowledge",
        "activation_mode": "command",
        "runtime_types": ["codex"],
    },
    "build": {
        "name": "Build & Run",
        "description": "Create, validate, version, update, and execute workflows in the current Chat.",
        "activation": "/workflow",
        "activation_mode": "command",
        "runtime_types": ["codex"],
    },
    "browser": {
        "name": "Browser",
        "description": "Control the user-approved browser page through the reviewed official Playwright MCP tool surface.",
        "activation": "/browser or the browser side panel",
        "activation_mode": "command",
        "runtime_types": ["codex"],
    },
}


SANDBOX_MCP_METADATA: Final[dict[str, dict[str, object]]] = {
    "diagram": {
        "name": "Diagram",
        "description": "Author, validate, present, visually review, and export semantic diagrams on an infinite canvas.",
        "activation": "/diagram",
        "activation_mode": "command",
        "runtime_types": ["codex"],
    },
    "document": {
        "name": "Document",
        "description": "Review and render professional native office documents inside the current Chat workspace.",
        "activation": "/document",
        "activation_mode": "command",
        "runtime_types": ["codex"],
    },
}


BUILTIN_MCP_METADATA: Final[dict[str, dict[str, object]]] = {
    **PLATFORM_MCP_METADATA,
    **SANDBOX_MCP_METADATA,
}

BUILTIN_MCP_CONTRACT_REVISION: Final[str] = "sha256:" + hashlib.sha256(
    json.dumps(
        BUILTIN_MCP_METADATA,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


def builtin_mcp_description(server: str) -> str:
    """Return the canonical Runtime-facing description for a built-in MCP."""
    metadata = BUILTIN_MCP_METADATA.get(server)
    if metadata is None:
        raise ValueError(f"unknown built-in MCP capability: {server}")
    return str(metadata["description"])


def sandbox_mcp_catalog() -> list[dict[str, object]]:
    """Describe credential-free MCP services started inside Chat sandboxes."""
    tool_sets = {
        "diagram": (
            DIAGRAM_MCP_TOOL_NAMES,
            "Provided by the pinned official draw.io MCP.",
        ),
        "document": (
            DOCUMENT_MCP_TOOL_NAMES,
            "Provided by the sandbox-contained Flowork document reviewer.",
        ),
    }
    return [
        {
            "id": server,
            **SANDBOX_MCP_METADATA[server],
            "tools": [
                {
                    "name": name,
                    "description": description,
                    "input_schema": {"type": "object"},
                }
                for name in names
            ],
        }
        for server, (names, description) in tool_sets.items()
    ]
