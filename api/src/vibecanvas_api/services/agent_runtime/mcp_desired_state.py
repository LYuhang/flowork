"""Host-only projection from durable selection to secret-free Hub contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .mcp_runtime_protocol import (
    McpBrokerConnection,
    McpDesiredServer,
    McpDesiredState,
    McpExecutionContext,
    McpPlatformFacade,
    McpStdioLaunch,
)
from .protocol import RuntimeTurnRequest
from vibecanvas_api.services.platform_mcp.catalog import (
    BUILTIN_MCP_CONTRACT_REVISION,
    BUILTIN_MCP_METADATA,
)


def _skill_catalog_revision(request: RuntimeTurnRequest) -> str:
    payload = [
        {
            "id": skill.skill_id,
            "name": skill.name,
            "revision": skill.revision_hash,
        }
        for skill in sorted(request.skills, key=lambda item: item.skill_id)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _builtin_server(name: str) -> McpDesiredServer:
    metadata = BUILTIN_MCP_METADATA[name]
    activation_mode = str(metadata["activation_mode"])
    if name == "diagram":
        return McpDesiredServer(
            id="builtin:diagram",
            source="builtin_local",
            name="diagram",
            description=str(metadata["description"]),
            configurationRevision=f"{BUILTIN_MCP_CONTRACT_REVISION}:diagram",
            required=True,
            activation="command",
            connection=McpStdioLaunch(
                command="flowork-diagram-mcp",
                args=[],
                cwd="/data",
                environmentProfile="diagram-local",
            ),
        )
    if name == "document":
        return McpDesiredServer(
            id="builtin:document",
            source="builtin_local",
            name="document",
            description=str(metadata["description"]),
            configurationRevision=f"{BUILTIN_MCP_CONTRACT_REVISION}:document",
            required=True,
            activation="command",
            connection=McpStdioLaunch(
                command="flowork-document-mcp",
                args=[],
                cwd="/data",
                environmentProfile="document-local",
            ),
        )
    if name == "browser":
        return McpDesiredServer(
            id="builtin:browser",
            source="builtin_local",
            name="browser",
            description=str(metadata["description"]),
            configurationRevision=f"{BUILTIN_MCP_CONTRACT_REVISION}:browser",
            required=True,
            activation="command",
            connection=McpStdioLaunch(
                command="flowork-playwright-mcp",
                args=[
                    "--codegen",
                    "none",
                    "--snapshot-mode",
                    "full",
                    "--timeout-action",
                    "7000",
                    "--timeout-navigation",
                    "60000",
                    "--timeout-settle",
                    "500",
                    "--output-dir",
                    "/data/browser-media",
                ],
                cwd="/data",
                environmentProfile="browser-gateway",
            ),
        )
    return McpDesiredServer(
        id=f"platform:{name}",
        source="platform",
        name=name,
        description=str(metadata["description"]),
        configurationRevision=f"{BUILTIN_MCP_CONTRACT_REVISION}:{name}",
        required=True,
        activation="base" if activation_mode == "base" else "command",
        connection=McpPlatformFacade(capability=name),
    )


def _selected_server(server) -> McpDesiredServer:
    connection = dict(server.connection)
    transport = str(connection.get("transport") or "")
    revision = server.config_revision or "unversioned"
    if transport == "stdio":
        if connection.get("env"):
            raise ValueError(
                f"secretless stdio MCP {server.name!r} cannot carry env"
            )
        desired_connection = McpStdioLaunch(
            command=str(connection.get("command") or ""),
            args=[str(value) for value in connection.get("args") or []],
            cwd=str(connection.get("cwd") or "/data"),
            environmentProfile="sandbox-default",
        )
        source = "custom_stdio"
    else:
        if not server.server_id:
            raise ValueError(f"remote MCP {server.name!r} has no installation id")
        desired_connection = McpBrokerConnection(
            transport=transport,
            brokerRoute=f"runtime-mcp:{server.server_id}",
            connectionTimeoutS=float(connection.get("timeout") or 30),
        )
        source = "custom_remote"
    return McpDesiredServer(
        id=f"installation:{server.server_id or server.name}",
        source=source,
        name=server.name,
        description=server.description,
        configurationRevision=revision,
        required=False,
        activation="selected",
        connection=desired_connection,
    )


def build_mcp_lifecycle_contracts(
    request: RuntimeTurnRequest,
    *,
    sandbox_id: str,
    sandbox_generation: int,
    authorization_generation: str,
    execution_capability: str,
    lifetime_s: int,
) -> tuple[McpDesiredState, McpExecutionContext]:
    """Build one complete Hub desired state and its Turn activation context."""
    runtime_type = request.runtime_type.value
    active_platform = set(request.active_platform_mcps)
    builtins = [
        _builtin_server(name)
        for name, metadata in sorted(BUILTIN_MCP_METADATA.items())
        if runtime_type in metadata["runtime_types"]
        and (
            metadata["activation_mode"] == "base"
            or name in active_platform
        )
    ]
    selected = [
        _selected_server(server)
        for server in request.mcp_host_servers
        if server.source == "custom"
    ]
    desired = McpDesiredState(
        organization_id=request.tenant_id,
        user_id=request.user_id,
        chat_id=request.chat_id,
        runtime_session_id=request.runtime_session_id,
        sandbox_id=sandbox_id,
        sandbox_generation=sandbox_generation,
        chat_mcp_config_revision=request.mcp_config_revision,
        platform_contract_revision=BUILTIN_MCP_CONTRACT_REVISION,
        skill_catalog_revision=_skill_catalog_revision(request),
        servers=[*builtins, *selected],
    )
    now = datetime.now(timezone.utc)
    context = McpExecutionContext(
        organization_id=request.tenant_id,
        user_id=request.user_id,
        chat_id=request.chat_id,
        runtime_session_id=request.runtime_session_id,
        sandbox_generation=sandbox_generation,
        turn_id=request.turn_id,
        agent_run_id=request.turn_id,
        execution_kind="chat_turn",
        active_commands=sorted(request.command_context.active_modes),
        active_platform_capabilities=list(request.active_platform_mcps),
        selected_mcp_revision=request.mcp_config_revision,
        approval_mode=request.approval_mode,
        surface=request.surface,
        authorization_generation=authorization_generation,
        issued_at=now,
        expires_at=now + timedelta(seconds=lifetime_s),
        capability=execution_capability,
    )
    return desired, context


__all__ = ["build_mcp_lifecycle_contracts"]
