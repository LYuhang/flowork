"""Resolve Host-only MCP authority for one accepted Agent Turn.

This module may access database rows and mint short-lived Host capabilities.
It is never imported by the sandbox Runtime. ``SandboxSession`` converts its
output into the secret-free desired state consumed by the sandbox-owned Hub.
"""

from __future__ import annotations

from collections.abc import Iterable

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.custom_mcp_capability import (
    mcp_config_revision,
    mint_runtime_custom_mcp_capability,
)
from vibecanvas_api.services.agent_runtime.model_capability import (
    authorization_model_generation,
)
from vibecanvas_api.services.agent_runtime.protocol import HostMcpServerAuthority
from vibecanvas_api.services.mcp_config import (
    server_descriptor,
    validate_mcp_connection_destination,
)
from vibecanvas_api.services.agent_resources.capability import (
    mint_agent_capability,
)
from vibecanvas_api.services.platform_mcp.catalog import builtin_mcp_description
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo


BASE_PLATFORM_MCPS = ("interactive", "cli")


def platform_mcp_names_for_modes(
    active_modes: Iterable[str],
    *,
    runtime_type: str | None = None,
) -> list[str]:
    """Return base capabilities plus command-activated capabilities."""
    modes = set(active_modes)
    command_servers: list[str] = []
    # All workflow/build/run tools now use the private CLI gateway. Keep the
    # workflow command's instructions, but do not start its retired empty MCPs:
    # required servers with no tools prevent the entire Agent turn from starting.
    command_servers.extend(
        # Browser CLI retains the private browser capability and live lease.
        # This private capability does not start a Browser MCP server.
        name for name in ("browser",) if name in modes
    )
    return [*BASE_PLATFORM_MCPS, *command_servers]


class McpSelectionError(ValueError):
    """A selected MCP cannot be resolved safely for this Turn."""


async def resolve_custom_mcp_authority(
    tenant_id: str,
    *,
    user_id: str,
    chat_id: str,
    turn_id: str,
    runtime_session_id: str,
    session_id: str,
    session_generation: int,
    membership_id: str,
    server_ids: Iterable[str],
) -> list[HostMcpServerAuthority]:
    """Resolve selected installations without exposing upstream credentials."""
    selected = {str(server_id) for server_id in server_ids}
    if not selected:
        return []
    async with session_scope(tenant_id=str(tenant_id)) as session:
        rows = await McpServersRepo(session).list_enabled_for_user(user_id)
        selected_rows = [row for row in rows if str(row["id"]) in selected]
    found = {str(row["id"]) for row in selected_rows}
    missing = sorted(selected - found)
    if missing:
        raise McpSelectionError(
            "Selected MCP servers are unavailable or not owned by this user: "
            + ", ".join(missing)
        )

    authorities: list[HostMcpServerAuthority] = []
    for row in sorted(selected_rows, key=lambda item: str(item["tool_prefix"])):
        serialized = server_descriptor({**row, "auth_config": {"type": "none"}})
        connection = serialized["connection"]
        try:
            await validate_mcp_connection_destination(connection)
        except ValueError as exc:
            raise McpSelectionError(
                f"MCP server {row['name']!r} has an unsafe endpoint: {exc}"
            ) from exc
        revision = mcp_config_revision(
            server_id=row["id"],
            updated_at=row.get("updated_at"),
        )
        transport = str(connection.get("transport") or "")
        if transport == "stdio":
            has_auth = (
                bool(row.get("auth_secret_ref"))
                or bool(row.get("connection_secret_ref"))
                or str(row.get("auth_mode") or "none") != "none"
                or str((row.get("auth_config") or {}).get("type") or "none")
                != "none"
                or bool(connection.get("env"))
            )
            if has_auth:
                raise McpSelectionError(
                    f"MCP server {row['name']!r} uses credentials that cannot "
                    "be exposed to the Chat sandbox"
                )
            runtime_connection = connection
        else:
            token = mint_runtime_custom_mcp_capability(
                organization_id=str(tenant_id),
                user_id=str(user_id),
                chat_id=str(chat_id),
                turn_id=str(turn_id),
                runtime_session_id=str(runtime_session_id),
                session_id=str(session_id),
                session_generation=int(session_generation),
                membership_id=str(membership_id),
                server_id=str(row["id"]),
                transport=transport,
                config_revision=revision,
                authorization_generation=authorization_model_generation(
                    model_id=config.openfga_authorization_model_id,
                ),
                secret=config.signing_secret,
                ttl_s=config.mcp.platform_capability_ttl_s,
            )
            runtime_connection = {
                "transport": transport,
                "url": (
                    f"{config.mcp.platform_internal_base_url}"
                    f"/api/internal/runtime-mcp/v1/{row['id']}"
                ),
                "headers": {"Authorization": f"Bearer {token}"},
            }
            for key in ("timeout", "sse_read_timeout", "terminate_on_close"):
                if key in connection:
                    runtime_connection[key] = connection[key]
        authorities.append(HostMcpServerAuthority(
            name=str(row["tool_prefix"]),
            source="custom",
            description=str(row.get("description") or ""),
            connection=runtime_connection,
            server_id=str(row["id"]),
            config_revision=revision,
        ))
    return authorities


def resolve_platform_mcp_authority(
    servers: Iterable[str],
    *,
    tenant_id: str,
    user_id: str,
    chat_id: str,
    turn_id: str,
    workspace_scope_id: str,
    runtime_session_id: str,
    session_id: str,
    session_generation: int,
    membership_id: str,
    approval_mode: str = "agent",
) -> list[HostMcpServerAuthority]:
    """Mint Host-only capabilities for privileged built-in MCP calls."""
    result: list[HostMcpServerAuthority] = []
    for server in servers:
        if server not in {"interactive", "cli", "browser"}:
            raise ValueError(f"unknown Agent platform capability: {server}")
        token = mint_agent_capability(
            organization_id=tenant_id,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            workspace_scope_id=workspace_scope_id,
            runtime_session_id=runtime_session_id,
            session_id=session_id,
            session_generation=session_generation,
            membership_id=membership_id,
            server=server,
            authorization_generation=authorization_model_generation(
                model_id=config.openfga_authorization_model_id,
            ),
            approval_mode=approval_mode,
            secret=config.signing_secret,
            ttl_s=config.mcp.platform_capability_ttl_s,
        )
        connection = {
            "transport": (
                "browser_gateway" if server == "browser" else "host_gateway"
            ),
            "capability": token,
        }
        result.append(HostMcpServerAuthority(
            name=server,
            source="platform",
            description=(f"Private {server} authority for the active turn."
                         if server in {"cli", "browser"} else builtin_mcp_description(server)),
            connection=connection,
            required=True,
        ))
    return result


__all__ = [
    "BASE_PLATFORM_MCPS",
    "McpSelectionError",
    "platform_mcp_names_for_modes",
    "resolve_custom_mcp_authority",
    "resolve_platform_mcp_authority",
]
