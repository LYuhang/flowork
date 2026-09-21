"""Stable host↔sandbox Agent Runtime protocol.

SDK-specific structures must be translated inside the sandbox adapter. They
must never leak into this module or the frontend event contract.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from .mcp_runtime_protocol import McpDesiredState, McpExecutionContext


RUNTIME_PROTOCOL_VERSION = 2


class RuntimeType(str, Enum):
    CODEX = "codex"


class RuntimeOpenRequest(BaseModel):
    protocol_version: Literal[2] = RUNTIME_PROTOCOL_VERSION
    tenant_id: str
    user_id: str
    chat_id: str
    runtime_type: RuntimeType
    runtime_session_id: str
    runtime_root: str
    state_ref: str | None = None
    runtime_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_runtime_root(self) -> "RuntimeOpenRequest":
        if not self.runtime_root.startswith("/runtime/"):
            raise ValueError("runtime_root must be under /runtime")
        if any(part in {"", ".", ".."} for part in self.runtime_root.split("/")[1:]):
            raise ValueError("runtime_root contains an invalid path segment")
        return self


class RuntimeSession(BaseModel):
    protocol_version: Literal[2] = RUNTIME_PROTOCOL_VERSION
    runtime_type: RuntimeType
    runtime_session_id: str
    state_ref: str | None = None
    runtime_version: int = Field(default=1, ge=1)


class HostMcpServerAuthority(BaseModel):
    """Host-only authority material used to build secret-free Hub state.

    The Host may keep internal broker URLs and short-lived bearer capabilities
    here. ``SandboxSession`` consumes this field and replaces it with
    ``McpDesiredState`` before the request crosses the Runtime bus.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    source: Literal["custom", "platform"]
    description: str = ""
    connection: dict[str, Any]
    server_id: str | None = None
    config_revision: str | None = None
    required: bool = False

    @model_validator(mode="after")
    def platform_servers_are_required(self) -> "HostMcpServerAuthority":
        if self.source == "platform":
            self.required = True
            if self.connection.get("transport") not in {
                "host_gateway",
                "browser_gateway",
            }:
                raise ValueError("Platform MCP authority requires a Host Gateway")
        elif self.connection.get("transport") in {
            "host_gateway",
            "browser_gateway",
        }:
            raise ValueError("Custom MCP authority cannot use the Platform Gateway")
        return self

    @field_validator("connection")
    @classmethod
    def validate_connection(cls, value: dict[str, Any]) -> dict[str, Any]:
        connection = dict(value or {})
        transport = str(connection.get("transport") or "")
        if transport in {"host_gateway", "browser_gateway"}:
            capability = str(connection.get("capability") or "")
            if not capability:
                raise ValueError("Host Gateway MCP authority requires a capability")
            if set(connection) != {"transport", "capability"}:
                raise ValueError("Host Gateway MCP authority contains unknown fields")
            return connection
        if transport == "streamable-http":
            transport = "streamable_http"
            connection["transport"] = transport
        if transport == "stdio":
            if not str(connection.get("command") or "").strip():
                raise ValueError("stdio MCP requires command")
            args = connection.get("args", [])
            if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
                raise ValueError("stdio MCP args must be a list of strings")
            env = connection.get("env")
            if env is not None and (
                not isinstance(env, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
            ):
                raise ValueError("stdio MCP env must contain string keys and values")
            return connection
        if transport not in {"sse", "streamable_http"}:
            raise ValueError(
                "MCP transport must be stdio, sse, or streamable_http"
            )
        url = str(connection.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("HTTP MCP requires an absolute http(s) URL")
        headers = connection.get("headers")
        if headers is not None and (
            not isinstance(headers, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
        ):
            raise ValueError("MCP headers must contain string keys and values")
        return connection


class RuntimeSkill(BaseModel):
    """Immutable Skill revision exposed through the Runtime's /skills mount."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    revision_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    root_path: str
    allowed_tools: list[str] = Field(default_factory=list)

    @field_validator("root_path")
    @classmethod
    def validate_root_path(cls, value: str) -> str:
        if not value.startswith("/skills/"):
            raise ValueError("Skill root_path must be under /skills")
        if any(part in {"", ".", ".."} for part in value.split("/")[1:]):
            raise ValueError("Skill root_path contains an invalid path segment")
        return value.rstrip("/")


class RuntimeCommandContext(BaseModel):
    """Platform-owned command metadata delivered to a Runtime Turn.

    Domain objects and backend implementation handles are intentionally absent:
    workflow graphs are fetched through Platform MCP and browser state through
    browser tools. ``extra='forbid'`` makes that boundary executable instead of
    relying on comments or adapter convention.
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str | None = None
    is_first: bool = False
    chat_context: str = ""
    workspace_scope_id: str = ""
    current_workflow_id: str | None = None
    agent_surface: Literal["chat", "browser"] = "chat"
    available_commands: list[str] = Field(default_factory=list)
    active_modes: list[str] = Field(default_factory=list)
    activated_this_turn: list[str] = Field(default_factory=list)


class RuntimeInstruction(BaseModel):
    """Backend-resolved instruction delivered to any Agent Runtime.

    Slash commands are platform control syntax. Runtime adapters must not parse
    them or import platform prompt registries; they only project these immutable
    instruction blocks into their SDK's native model-input representation.
    """

    model_config = ConfigDict(extra="forbid")

    instruction_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    kind: Literal["command_context"]
    scope: Literal["chat"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: int = Field(ge=1)
    content: str = Field(min_length=1)
    activated_this_turn: bool = False


class RuntimeContextSection(BaseModel):
    """One ordered, observable context resource; never contains secret text."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal[
        "system", "policy", "workspace", "memory", "history",
        "tool_schema", "tool_result", "artifact_ref", "plan",
    ]
    source: str
    priority: int = Field(ge=0, le=100)
    token_estimate: int = Field(ge=0)
    retention: Literal["pinned", "summarize", "evictable", "reloadable"]
    content_hash: str | None = None


class RuntimeContextManifest(BaseModel):
    """Product-level context inventory shared by all Runtime adapters."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    mode: Literal["off", "shadow", "active"] = "shadow"
    adapter_mode: Literal["observe_only", "active", "native"] = "observe_only"
    budget: dict[str, int] = Field(default_factory=lambda: {
        "max_tokens": 200_000,
        "target_tokens": 100_000,
    })
    sections: list[RuntimeContextSection] = Field(default_factory=list)
    ordered_hash: str = ""


class RuntimeConversationClock(BaseModel):
    """Immutable wall-clock reference fixed by the first Runtime Turn."""

    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(min_length=1, max_length=128)
    started_at: datetime


class RuntimeDurableToolCall(BaseModel):
    """Bounded, provider-neutral tool call retained in product history."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(default="", max_length=512)
    name: str = Field(default="", max_length=256)
    arguments: str = Field(default="", max_length=32_768)


class RuntimeDurableAttachment(BaseModel):
    """Non-secret attachment reference suitable for Runtime recovery."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=512)
    path: str = Field(default="", max_length=2_048)
    media_type: str = Field(default="", max_length=256)


class RuntimeDurableHistoryMessage(BaseModel):
    """One completed product-transcript entry used only for recovery."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=1_024)
    turn_id: str | None = Field(default=None, max_length=1_024)
    role: Literal["user", "assistant", "tool", "system"]
    text: str = Field(default="", max_length=32_768)
    tool_calls: list[RuntimeDurableToolCall] = Field(
        default_factory=list,
        max_length=32,
    )
    tool_call_id: str | None = Field(default=None, max_length=512)
    attachments: list[RuntimeDurableAttachment] = Field(
        default_factory=list,
        max_length=32,
    )
    status: str | None = Field(default=None, max_length=128)


class RuntimeDurableHistorySnapshot(BaseModel):
    """Backend-owned bounded transcript for reconstructing lost native state.

    The native Runtime thread remains the ordinary fast path. This snapshot is
    deliberately carried on every Turn so a newly provisioned sandbox can
    recover without direct database access. Runtime adapters must treat its
    contents as untrusted conversation/tool data, never as platform policy.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[RuntimeDurableHistoryMessage] = Field(
        default_factory=list,
        max_length=512,
    )
    last_turn_id: str | None = Field(default=None, max_length=1_024)
    truncated: bool = False
    omitted_message_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_wire_budget(self) -> "RuntimeDurableHistorySnapshot":
        encoded = self.model_dump_json().encode("utf-8")
        if len(encoded) > 768 * 1024:
            raise ValueError("durable history exceeds the Runtime wire budget")
        return self


class RuntimeTurnRequest(BaseModel):
    protocol_version: Literal[2] = RUNTIME_PROTOCOL_VERSION
    tenant_id: str
    user_id: str
    chat_id: str
    turn_id: str
    runtime_type: RuntimeType
    runtime_session_id: str
    # Private adapter namespace inside the sandbox. This value is created by
    # the platform from the immutable Chat binding; it is never user input and
    # is never included in frontend projections.
    runtime_root: str
    runtime_state_ref: str | None = None
    # Host-only bounded continuation index within one durable product Turn.
    # Zero is the user's native Runtime turn; positive values are deterministic
    # protocol continuations and must never be projected as new user messages.
    continuation_index: int = Field(default=0, ge=0, le=3)
    # A Runtime receives the same value on every Turn/resume so its system
    # prefix remains byte-stable. Codex owns its native context and never
    # receives this platform prompt block.
    conversation_clock: RuntimeConversationClock | None = None
    # PostgreSQL is the authoritative product transcript. Native SDK threads
    # are an optimization and may disappear with a sandbox; adapters consume
    # this bounded snapshot only when native coverage cannot be proven.
    durable_history: RuntimeDurableHistorySnapshot = Field(
        default_factory=RuntimeDurableHistorySnapshot
    )
    message: dict[str, Any]
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    model: dict[str, Any] = Field(default_factory=dict)
    # Runtime catalogs own this vocabulary. Codex app-server deliberately
    # advertises arbitrary non-empty strings and newer clients may receive
    # values that did not exist when the platform was released.
    reasoning_effort: str | None = None
    approval_mode: Literal["agent", "always_ask", "always_allow"] = "agent"
    surface: Literal["main", "sidepanel"] = "main"
    active_platform_mcps: list[
        Literal[
            "config",
            "interactive",
            "workflow",
            "task",
            "deployment",
            "knowledge",
            "build",
            "browser",
            "diagram",
            "document",
        ]
    ] = Field(default_factory=list)
    mcp_config_revision: int = Field(default=0, ge=0)
    mcp_host_servers: list[HostMcpServerAuthority] = Field(default_factory=list)
    # The Host first resolves credential-bearing authority; SandboxSession then
    # replaces it with secret-free Hub lifecycle contracts before serialization.
    # No Runtime adapter accepts Host-stage MCP descriptors.
    mcp_runtime_stage: Literal["host", "sandbox"] = "host"
    mcp_desired_state: McpDesiredState | None = None
    mcp_execution_context: McpExecutionContext | None = None
    skills: list[RuntimeSkill] = Field(default_factory=list)
    # Backend-owned full Todo snapshot. Runtime adapters may mutate a turn-local
    # copy, but every update is projected back to the backend and the next Turn
    # starts from this source rather than an SDK checkpoint.
    todo_items: list[dict[str, Any]] = Field(default_factory=list)
    todo_revision: int = Field(default=0, ge=0)
    # Backend-owned compact projection of durable interactive artifacts.  Like
    # Todo state, this crosses the private Runtime bus on every Turn so a
    # sandbox can reconcile its SDK checkpoint without direct database access.
    interactive_artifact_refs: dict[str, dict[str, Any]] = Field(
        default_factory=dict
    )
    instructions: list[RuntimeInstruction] = Field(default_factory=list)
    command_context: RuntimeCommandContext = Field(
        default_factory=RuntimeCommandContext
    )
    context_manifest: RuntimeContextManifest = Field(
        default_factory=RuntimeContextManifest
    )

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("reasoning_effort must be a non-empty string")
        return normalized

    @model_validator(mode="after")
    def validate_private_runtime_root(self) -> "RuntimeTurnRequest":
        if not self.runtime_root.startswith("/runtime/"):
            raise ValueError("runtime_root must be under /runtime")
        if any(part in {"", ".", ".."} for part in self.runtime_root.split("/")[1:]):
            raise ValueError("runtime_root contains an invalid path segment")
        if self.runtime_type == RuntimeType.CODEX:
            if self.conversation_clock is not None:
                raise ValueError(
                    "Codex Runtime owns conversation time context"
                )
            account_mode = self.model.get("connection_type") == "chatgpt_account"
            required_model_fields = (
                ("id",) if account_mode else ("id", "base_url", "api_key")
            )
            if any(
                not isinstance(self.model.get(field), str)
                or not self.model[field].strip()
                for field in required_model_fields
            ):
                raise ValueError(
                    "Codex Runtime requires a host-brokered model capability"
                )
            if account_mode and any(
                self.model.get(field) for field in ("base_url", "api_key")
            ):
                raise ValueError(
                    "Codex account mode must not include a broker credential"
                )
        active = list(self.active_platform_mcps)
        if len(active) != len(set(active)):
            raise ValueError("active_platform_mcps must not contain duplicates")
        attached = [
            server.name
            for server in self.mcp_host_servers
            if server.source == "platform"
        ]
        if len(attached) != len(set(attached)):
            raise ValueError("platform MCP descriptors must not contain duplicates")
        if self.mcp_runtime_stage == "host":
            if self.mcp_desired_state is not None or self.mcp_execution_context is not None:
                raise ValueError("Host-stage MCP request cannot carry Hub contracts")
            host_backed = set(active) - {"diagram", "document"}
            if set(attached) != host_backed:
                raise ValueError(
                    "Host Platform MCP authority must exactly match active capabilities"
                )
        else:
            desired = self.mcp_desired_state
            execution = self.mcp_execution_context
            if desired is None or execution is None:
                raise ValueError(
                    "Sandbox-stage MCP request requires desired state and execution context"
                )
            request_identity = (
                self.tenant_id,
                self.user_id,
                self.chat_id,
                self.runtime_session_id,
            )
            if request_identity != (
                desired.organization_id,
                desired.user_id,
                desired.chat_id,
                desired.runtime_session_id,
            ) or request_identity != (
                execution.organization_id,
                execution.user_id,
                execution.chat_id,
                execution.runtime_session_id,
            ):
                raise ValueError(
                    "MCP lifecycle contracts must match the Runtime Turn identity"
                )
            if (
                execution.selected_mcp_revision
                != desired.chat_mcp_config_revision
            ):
                raise ValueError(
                    "MCP execution context must target the desired-state revision"
                )
            if set(execution.active_platform_capabilities) != set(active):
                raise ValueError(
                    "MCP execution capabilities must match active_platform_mcps"
                )
            if self.mcp_host_servers:
                raise ValueError(
                    "Sandbox-stage MCP request cannot carry Host authority descriptors"
                )
        skill_names = [skill.name.casefold() for skill in self.skills]
        if len(skill_names) != len(set(skill_names)):
            raise ValueError("runtime skills must not contain duplicate names")
        instruction_ids = [item.instruction_id for item in self.instructions]
        if len(instruction_ids) != len(set(instruction_ids)):
            raise ValueError("runtime instructions must not contain duplicate ids")
        command_names = {
            item.name
            for item in self.instructions
            if item.kind == "command_context"
        }
        if command_names != set(self.command_context.active_modes):
            raise ValueError(
                "command_context instructions must exactly match active_modes"
            )
        activated_instruction_names = {
            item.name
            for item in self.instructions
            if item.kind == "command_context" and item.activated_this_turn
        }
        if activated_instruction_names != set(
            self.command_context.activated_this_turn
        ):
            raise ValueError(
                "activated command instructions must exactly match "
                "activated_this_turn"
            )
        return self


class RuntimeCapabilitiesRequest(BaseModel):
    """Request a user/account-aware runtime configuration catalog."""

    protocol_version: Literal[2] = RUNTIME_PROTOCOL_VERSION
    tenant_id: str
    user_id: str
    runtime_type: RuntimeType
    runtime_root: str
    force_refresh: bool = False

    @model_validator(mode="after")
    def validate_runtime_root(self) -> "RuntimeCapabilitiesRequest":
        if not self.runtime_root.startswith("/runtime/"):
            raise ValueError("runtime_root must be under /runtime")
        if any(part in {"", ".", ".."} for part in self.runtime_root.split("/")[1:]):
            raise ValueError("runtime_root contains an invalid path segment")
        return self


class RuntimeReasoningEffortOption(BaseModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ""


class RuntimeModelOption(BaseModel):
    """One model choice in runtime-defined display order.

    ``id`` is the stable value sent back on a Turn. It may be an account model
    slug or an opaque platform selection id.
    """

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ""
    api_source: str | None = None
    api_protocol: str | None = None
    provider: str | None = None
    provider_model_id: str | None = None
    context_length: int | None = Field(default=None, gt=0)
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    supports_tools: bool | None = None
    supports_web_search: bool = False
    input_price: str | None = None
    output_price: str | None = None
    available: bool = True
    is_default: bool = False
    supported_reasoning_efforts: list[RuntimeReasoningEffortOption] = Field(
        default_factory=list
    )
    default_reasoning_effort: str | None = None


class RuntimeCapabilities(BaseModel):
    protocol_version: Literal[2] = RUNTIME_PROTOCOL_VERSION
    runtime_type: RuntimeType
    runtime_available: bool
    authenticated: bool | None = None
    source: str
    models: list[RuntimeModelOption] = Field(default_factory=list)
    default_model_id: str | None = None
    error_code: str | None = None
    # Chat projection for the composer. Bound settings are the last accepted
    # selection used to seed Resume; model and effort remain mutable within the
    # Chat's fixed connection between idle Turns.
    bound_agent_settings: dict[str, Any] | None = None


RUNTIME_EVENT_TYPES = Literal[
    "runtime.started",
    "runtime.completed",
    "runtime.failed",
    "message.start",
    "message.delta",
    "message.end",
    "tool.start",
    "tool.update",
    "tool.end",
    "approval.requested",
    "approval.required",
    "approval.resolved",
    "interaction.required",
    "interaction.resolved",
    "artifact",
    "usage",
    "checkpoint",
    # Private sandbox MCP Hub→Host Gateway request. Tool manifests and calls
    # cross the Chat-bound Runtime bus; Platform URLs and bearer credentials do
    # not. The orchestrator consumes this event and returns a private control
    # response, so it is never projected to product history or the frontend.
    "mcp.gateway.requested",
    # Product-level events that do not fit the portable message/tool primitives
    # (workflow patches, todo projections, rich debug frames, etc.).  The payload
    # contains the platform event name + JSON payload, never an SDK event object.
    "projection",
]


class RuntimeEvent(BaseModel):
    protocol_version: Literal[2] = RUNTIME_PROTOCOL_VERSION
    event_id: str
    seq: int = Field(ge=1)
    chat_id: str
    turn_id: str
    runtime_type: RuntimeType
    runtime_session_id: str
    type: RUNTIME_EVENT_TYPES
    payload: dict[str, Any] = Field(default_factory=dict)


class RuntimeRequestCorrelation(BaseModel):
    """Runtime-private coordinates for one suspended server request.

    ``request_id`` on :class:`RuntimeControlResponse` is the stable platform
    HITL id.  These fields identify the SDK-native wait point that the sandbox
    adapter must answer.  They are persisted in ``hitl_requests`` and never
    supplied by, or exposed as authoritative state to, the frontend.
    """

    # Extensible Runtime adapter identifier. Adding a Runtime must not require
    # changing the product control protocol merely to add another source enum.
    source: str = Field(min_length=1)
    runtime_request_id: str | int
    runtime_method: str = Field(min_length=1)
    runtime_thread_id: str | None = None
    runtime_turn_id: str | None = None
    runtime_item_id: str | None = None
    runtime_approval_id: str | None = None


class RuntimeMcpGatewayRequest(BaseModel):
    """Typed private request from the sandbox Hub to the trusted Host."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=256)
    operation: Literal[
        "manifest",
        "call",
        "launch",
        "remote_message",
        "remote_close",
    ]
    server: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tool_name: str | None = Field(default=None, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)
    execution_capability: SecretStr
    runtime_correlation: RuntimeRequestCorrelation


class RuntimeControlResponse(BaseModel):
    protocol_version: Literal[2] = RUNTIME_PROTOCOL_VERSION
    # Stable platform hitl_request_id. It is deliberately distinct from the
    # Runtime-native request id (for Codex this is its JSON-RPC request id).
    request_id: str
    chat_id: str
    turn_id: str
    gate_type: Literal["pre_tool_approval", "post_tool_interaction"]
    action: Literal["approve", "deny", "submit", "cancel"]
    # True only when the host created a durable HITL request visible to users.
    # Runtimes avoid emitting a spurious resolved UI event for an immediate
    # policy allow/deny.
    persisted: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation: RuntimeRequestCorrelation

    @model_validator(mode="after")
    def validate_action_for_gate(self) -> "RuntimeControlResponse":
        allowed = {
            "pre_tool_approval": {"approve", "deny", "cancel"},
            "post_tool_interaction": {"submit", "cancel"},
        }
        if self.action not in allowed[self.gate_type]:
            raise ValueError(
                f"action {self.action!r} is invalid for gate {self.gate_type!r}"
            )
        return self


class RuntimeMcpGatewayResponse(BaseModel):
    """Host response to one sandbox-local MCP Hub gateway request."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[2] = RUNTIME_PROTOCOL_VERSION
    request_id: str
    chat_id: str
    turn_id: str
    control_type: Literal["mcp_gateway"] = "mcp_gateway"
    operation: Literal[
        "manifest",
        "call",
        "launch",
        "remote_message",
        "remote_close",
    ]
    action: Literal["accepted", "rejected"]
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    correlation: RuntimeRequestCorrelation

    @model_validator(mode="after")
    def validate_result(self) -> "RuntimeMcpGatewayResponse":
        if self.action == "rejected" and not self.error:
            raise ValueError("rejected MCP Gateway request requires error")
        return self


RuntimeControlMessage = (
    RuntimeControlResponse
    | RuntimeMcpGatewayResponse
)


@runtime_checkable
class SandboxAgentRuntime(Protocol):
    async def capabilities(
        self, request: RuntimeCapabilitiesRequest
    ) -> RuntimeCapabilities: ...

    async def open(self, request: RuntimeOpenRequest) -> RuntimeSession: ...

    def run_turn(self, request: RuntimeTurnRequest) -> AsyncIterator[RuntimeEvent]: ...

    async def respond(self, response: RuntimeControlMessage) -> None: ...

    async def cancel(self, turn_id: str) -> bool: ...

    async def close(self) -> None: ...
