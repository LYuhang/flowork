"""Secret-free Host-to-sandbox MCP Runtime contracts.

These models describe desired MCP topology and short-lived execution authority
without embedding upstream credentials or model-visible Host MCP URLs. They are
safe to serialize across the private sandbox bus; authorization is still owned
and revalidated by the trusted Host.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


MCP_RUNTIME_PROTOCOL_VERSION = 1
PlatformMcpName = Literal["interactive"]
AgentPlatformCapability = Literal["interactive", "cli", "browser"]


class McpRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class McpPlatformFacade(McpRuntimeModel):
    kind: Literal["platform_facade"] = "platform_facade"
    capability: PlatformMcpName


class McpBrokerConnection(McpRuntimeModel):
    kind: Literal["host_broker"] = "host_broker"
    transport: Literal["sse", "streamable_http"]
    broker_route: str = Field(
        alias="brokerRoute",
        min_length=1,
        max_length=256,
        pattern=r"^runtime-mcp:[A-Za-z0-9_.:-]+$",
    )
    connection_timeout_s: float = Field(
        default=30,
        alias="connectionTimeoutS",
        ge=1,
        le=300,
    )


class McpStdioLaunch(McpRuntimeModel):
    kind: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1, max_length=512)
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str = Field(default="/data", min_length=1, max_length=512)
    environment_profile: str = Field(
        default="sandbox-default",
        alias="environmentProfile",
        pattern=r"^[a-z][a-z0-9-]{0,63}$",
    )

    @field_validator("cwd")
    @classmethod
    def cwd_stays_in_sandbox_workspace(cls, value: str) -> str:
        allowed = ("/data", "/memory", "/chats", "/runtime", "/skills", "/tmp")
        if not any(value == root or value.startswith(f"{root}/") for root in allowed):
            raise ValueError("stdio MCP cwd must stay inside a sandbox workspace root")
        if any(part in {"", ".", ".."} for part in value.split("/")[1:]):
            raise ValueError("stdio MCP cwd contains an invalid path segment")
        return value


McpDesiredConnection = Annotated[
    McpPlatformFacade | McpBrokerConnection | McpStdioLaunch,
    Field(discriminator="kind"),
]


class McpDesiredServer(McpRuntimeModel):
    id: str = Field(min_length=1, max_length=256)
    source: Literal[
        "platform",
        "custom_remote",
        "custom_stdio",
    ]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(default="", max_length=2000)
    configuration_revision: str = Field(
        alias="configurationRevision",
        min_length=1,
        max_length=256,
    )
    required: bool = False
    activation: Literal["base", "command", "selected"]
    connection: McpDesiredConnection

    @model_validator(mode="after")
    def source_matches_connection(self) -> "McpDesiredServer":
        expected = {
            "platform": "platform_facade",
            "custom_remote": "host_broker",
            "custom_stdio": "stdio",
        }[self.source]
        if self.connection.kind != expected:
            raise ValueError(
                f"{self.source} MCP requires a {expected} connection"
            )
        if self.source == "platform":
            self.required = True
            if self.connection.capability != self.name:
                raise ValueError("Platform facade capability must match server name")
        return self


class McpDesiredState(McpRuntimeModel):
    protocol_version: Literal[1] = MCP_RUNTIME_PROTOCOL_VERSION
    organization_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    runtime_session_id: str = Field(min_length=1)
    sandbox_id: str = Field(min_length=1)
    sandbox_generation: int = Field(ge=1)
    project_mcp_config_revision: int = Field(ge=0)
    platform_contract_revision: str = Field(min_length=1, max_length=256)
    skill_catalog_revision: str = Field(min_length=1, max_length=256)
    servers: list[McpDesiredServer] = Field(default_factory=list, max_length=128)
    # Host-approved private CLI authority does not create an MCP server or tool
    # manifest. Keep this explicit in desired state so activation cannot expand
    # privileges merely by naming a retired/nonexistent MCP in the context.
    private_platform_capabilities: list[Literal["cli", "browser"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_server_identity(self) -> "McpDesiredState":
        ids = [server.id for server in self.servers]
        names = [server.name for server in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP desired server ids must be unique")
        if len(names) != len(set(names)):
            raise ValueError("MCP desired server names must be unique")
        return self

    @property
    def revision_key(self) -> tuple[object, ...]:
        return (
            self.sandbox_generation,
            self.project_mcp_config_revision,
            self.platform_contract_revision,
            self.skill_catalog_revision,
            tuple(sorted(self.private_platform_capabilities)),
            tuple(
                (server.id, server.configuration_revision)
                for server in self.servers
            ),
        )


class McpSandboxIdentity(McpRuntimeModel):
    protocol_version: Literal[1] = MCP_RUNTIME_PROTOCOL_VERSION
    organization_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    runtime_session_id: str = Field(min_length=1)
    sandbox_id: str = Field(min_length=1)
    sandbox_generation: int = Field(ge=1)
    membership_id: str = Field(min_length=1)
    authorization_generation: str = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime
    capability: SecretStr

    @model_validator(mode="after")
    def expiry_is_forward(self) -> "McpSandboxIdentity":
        if self.expires_at <= self.issued_at:
            raise ValueError("sandbox identity must expire after it is issued")
        return self


class McpExecutionContext(McpRuntimeModel):
    protocol_version: Literal[1] = MCP_RUNTIME_PROTOCOL_VERSION
    organization_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    runtime_session_id: str = Field(min_length=1)
    sandbox_generation: int = Field(ge=1)
    turn_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)
    execution_kind: Literal["chat_turn", "background_job"] = "chat_turn"
    active_commands: list[str] = Field(default_factory=list, max_length=32)
    active_platform_capabilities: list[AgentPlatformCapability] = Field(
        default_factory=list,
        max_length=16,
    )
    selected_mcp_revision: int = Field(ge=0)
    approval_mode: Literal["agent", "always_ask", "always_allow"] = "agent"
    surface: Literal["main", "sidepanel", "background"] = "main"
    authorization_generation: str = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime
    capability: SecretStr

    @model_validator(mode="after")
    def validate_context(self) -> "McpExecutionContext":
        if self.expires_at <= self.issued_at:
            raise ValueError("MCP execution context must expire after it is issued")
        if len(self.active_platform_capabilities) != len(
            set(self.active_platform_capabilities)
        ):
            raise ValueError("active Platform MCP capabilities must be unique")
        return self

    def is_expired(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return current >= expiry


class McpServerStatus(McpRuntimeModel):
    id: str
    name: str
    source: Literal[
        "platform",
        "custom_remote",
        "custom_stdio",
    ]
    state: Literal[
        "disabled",
        "starting",
        "ready",
        "degraded",
        "reconnecting",
        "authorization_required",
        "configuration_stale",
        "failed",
        "stopping",
        "stopped",
    ]
    tool_count: int = Field(default=0, ge=0)
    configuration_revision: str
    manifest_revision: str | None = None
    last_error_code: str | None = None


class McpReconcileResult(McpRuntimeModel):
    desired_revision: int = Field(ge=0)
    applied_revision: int = Field(ge=0)
    required_ready: bool
    changed_server_ids: list[str] = Field(default_factory=list)
    removed_server_ids: list[str] = Field(default_factory=list)
    servers: list[McpServerStatus] = Field(default_factory=list)
    cache_used: bool = False
    network_sessions_reinitialized: bool = False


class McpHubStatus(McpRuntimeModel):
    protocol_version: Literal[1] = MCP_RUNTIME_PROTOCOL_VERSION
    sandbox_generation: int = Field(ge=1)
    config_revision: int = Field(ge=0)
    execution_state: Literal["inactive", "active", "draining"] = "inactive"
    active_call_count: int = Field(default=0, ge=0)
    servers: list[McpServerStatus] = Field(default_factory=list)


__all__ = [
    "MCP_RUNTIME_PROTOCOL_VERSION",
    "McpBrokerConnection",
    "McpDesiredServer",
    "McpDesiredState",
    "McpExecutionContext",
    "McpHubStatus",
    "McpPlatformFacade",
    "McpReconcileResult",
    "McpSandboxIdentity",
    "McpServerStatus",
    "McpStdioLaunch",
    "PlatformMcpName",
]
