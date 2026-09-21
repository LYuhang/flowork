from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.mcp_host_resolution import (
    platform_mcp_names_for_modes,
    resolve_platform_mcp_authority,
)
from vibecanvas_api.services.agent_runtime.protocol import HostMcpServerAuthority
from vibecanvas_api.services.platform_mcp.capability import (
    verify_platform_mcp_capability,
)


def _platform_authority(names: list[str], *, runtime_session_id: str):
    return resolve_platform_mcp_authority(
        names,
        tenant_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        chat_id="chat-runtime-neutral",
        turn_id="turn-runtime-neutral",
        workspace_scope_id="workspace-runtime-neutral",
        runtime_session_id=runtime_session_id,
        session_id="33333333-3333-3333-3333-333333333333",
        session_generation=4,
        membership_id="44444444-4444-4444-4444-444444444444",
    )


def test_platform_mcp_selection_is_command_driven_and_stable() -> None:
    base = ["config", "interactive"]
    assert platform_mcp_names_for_modes([]) == base
    assert platform_mcp_names_for_modes([], runtime_type="codex") == base
    assert platform_mcp_names_for_modes(["workflow"]) == [
        *base,
        "workflow",
        "build",
    ]
    assert platform_mcp_names_for_modes(["browser"]) == [*base, "browser"]
    assert platform_mcp_names_for_modes(["diagram"]) == [*base, "diagram"]
    assert platform_mcp_names_for_modes(["document"]) == [*base, "document"]
    assert platform_mcp_names_for_modes(["browser", "workflow"]) == [
        *base,
        "workflow",
        "build",
        "browser",
    ]
    assert platform_mcp_names_for_modes(
        ["knowledge", "deployment", "task"]
    ) == [*base, "workflow", "task", "deployment", "knowledge"]
    assert platform_mcp_names_for_modes(["task"]) == [
        *base,
        "workflow",
        "task",
    ]
    assert platform_mcp_names_for_modes(["deployment"]) == [
        *base,
        "workflow",
        "deployment",
    ]


def test_platform_mcp_authorization_is_bound_to_runtime_session() -> None:
    authority = _platform_authority(
        ["workflow"], runtime_session_id="runtime-codex"
    )[0]
    capability = verify_platform_mcp_capability(
        authority.connection["capability"],
        secret=config.signing_secret,
        server="workflow",
    )
    assert capability is not None
    assert capability.runtime_session_id == "runtime-codex"


def test_browser_authority_is_host_only_and_turn_scoped() -> None:
    authority = _platform_authority(
        ["browser"],
        runtime_session_id="runtime-browser",
    )[0]
    connection = authority.connection
    assert connection["transport"] == "browser_gateway"
    capability = verify_platform_mcp_capability(
        connection["capability"],
        secret=config.signing_secret,
        server="browser",
    )
    assert capability is not None
    assert capability.runtime_session_id == "runtime-browser"


def test_diagram_requires_no_host_authority() -> None:
    assert _platform_authority(
        ["diagram"],
        runtime_session_id="runtime-diagram",
    ) == []


def test_document_requires_no_host_authority() -> None:
    assert _platform_authority(
        ["document"],
        runtime_session_id="runtime-document",
    ) == []


def test_host_authority_accepts_supported_connections() -> None:
    stdio = HostMcpServerAuthority(
        name="files",
        source="custom",
        connection={"transport": "stdio", "command": "mcp-files", "args": []},
    )
    remote = HostMcpServerAuthority(
        name="github",
        source="custom",
        connection={
            "transport": "streamable-http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer secret"},
        },
    )
    assert stdio.connection["transport"] == "stdio"
    assert remote.connection["transport"] == "streamable_http"


@pytest.mark.parametrize(
    "connection",
    [
        {"transport": "stdio", "args": []},
        {"transport": "websocket", "url": "wss://example.test/mcp"},
        {"transport": "streamable_http", "url": "/relative/mcp"},
        {
            "transport": "streamable_http",
            "url": "https://example.test/mcp",
            "headers": {"X-Test": 1},
        },
    ],
)
def test_host_authority_rejects_invalid_connections(connection: dict) -> None:
    with pytest.raises(ValidationError):
        HostMcpServerAuthority(
            name="bad",
            source="custom",
            connection=connection,
        )
