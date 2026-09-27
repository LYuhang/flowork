from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.mcp_host_resolution import (
    platform_mcp_names_for_modes,
    resolve_platform_mcp_authority,
)
from vibecanvas_api.services.agent_runtime.protocol import HostMcpServerAuthority
from vibecanvas_api.services.agent_resources.capability import (
    verify_agent_capability,
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
    base = ["interactive", "cli"]
    assert platform_mcp_names_for_modes([]) == base
    assert platform_mcp_names_for_modes([], runtime_type="codex") == base
    assert platform_mcp_names_for_modes(["workflow"]) == base
    assert platform_mcp_names_for_modes(["browser"]) == [*base, "browser"]
    assert platform_mcp_names_for_modes(["diagram"]) == base
    assert platform_mcp_names_for_modes(["document"]) == base
    assert platform_mcp_names_for_modes(["browser", "workflow"]) == [
        *base,
        "browser",
    ]
    assert platform_mcp_names_for_modes(
        ["knowledge", "deployment", "task"]
    ) == base
    assert platform_mcp_names_for_modes(["task"]) == base
    assert platform_mcp_names_for_modes(["deployment"]) == [
        *base,
    ]


def test_workflow_activation_keeps_guidance_without_empty_required_mcps() -> None:
    from vibecanvas_api.services.agent_runtime.instructions import command_instructions_for_modes
    from vibecanvas_api.services.platform_mcp.invocation import platform_mcp_tool_manifest

    names = platform_mcp_names_for_modes(["workflow", "task", "deployment", "knowledge"])
    assert not {"build", "workflow"}.intersection(names)
    for name in set(names) - {"cli", "browser"}:
        assert platform_mcp_tool_manifest(name), f"Required MCP {name} has no tools"
    instructions = command_instructions_for_modes({"workflow"}, activated_this_turn={"workflow"})
    assert len(instructions) == 1
    assert instructions[0].activated_this_turn
    assert "flowork-cli workflow" in instructions[0].content


def test_platform_mcp_authorization_is_bound_to_runtime_session() -> None:
    authority = _platform_authority(
        ["cli"], runtime_session_id="runtime-codex"
    )[0]
    capability = verify_agent_capability(
        authority.connection["capability"],
        secret=config.signing_secret,
        server="cli",
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
    capability = verify_agent_capability(
        connection["capability"],
        secret=config.signing_secret,
        server="browser",
    )
    assert capability is not None
    assert capability.runtime_session_id == "runtime-browser"


@pytest.mark.parametrize("name", ["diagram", "document", "workflow", "build", "knowledge", "deployment", "config", "task"])
def test_retired_servers_cannot_receive_authority(name) -> None:
    with pytest.raises(ValueError):
        _platform_authority([name], runtime_session_id="runtime-retired")


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
