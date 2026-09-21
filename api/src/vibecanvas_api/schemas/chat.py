"""Chat-related schemas."""

from __future__ import annotations

from datetime import datetime
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

from vibecanvas_api.schemas.access import ResourceAccessOut


class ChatListItem(BaseModel):
    chat_id: str
    scope_id: str
    surface: Literal["chat", "browser"] = "chat"
    chat_context: str = ""
    created_at: str = ""
    browser_control_status: Literal[
        "inactive", "attaching", "attached", "lost",
    ] = "inactive"
    runtime_type: str | None = None
    access: ResourceAccessOut | None = None


class ChatRenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("chat name must not be empty")
        if len(normalized) > 120:
            raise ValueError("chat name is too long")
        return normalized


class ChatInventoryItem(BaseModel):
    """Content-free organization inventory projection for private Chats."""

    chat_id: str
    scope_id: str
    surface: Literal["chat", "browser"] = "chat"
    runtime_type: str | None = None
    browser_control_status: Literal[
        "inactive", "attaching", "attached", "lost",
    ] = "inactive"
    created_at: str
    updated_at: str
    last_message_at: str | None = None
    access: ResourceAccessOut


class TodoItem(BaseModel):
    id: int
    text: str
    status: Literal["pending", "in_progress", "done"]


class BackgroundJobProgress(BaseModel):
    current: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    message: str = ""


class BackgroundJobOut(BaseModel):
    job_id: str
    chat_id: str
    parent_run_id: str | None = None
    runtime_type: str
    executor_type: str
    tool_name: str
    title: str = ""
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
    ]
    progress: BackgroundJobProgress = Field(default_factory=BackgroundJobProgress)
    input: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    result_ref: str | None = None
    error: dict = Field(default_factory=dict)
    event_seq: int = Field(default=0, ge=0)
    cancel_requested: bool = False
    delivery_status: Literal["pending", "delivered"] = "pending"
    delivered_at: str | None = None
    delivery_batch_id: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None


class BackgroundJobCancelBody(BaseModel):
    reason: str = Field(default="user_requested", max_length=256)


class ChatStateOut(BaseModel):
    todo_items: list[TodoItem] = Field(default_factory=list)
    background_jobs: list[BackgroundJobOut] = Field(default_factory=list)
    active_modes: list[str] = Field(default_factory=list)
    mcp_server_ids: list[str] = Field(default_factory=list)
    mcp_config_revision: int = Field(default=0, ge=0)


class Attachment(BaseModel):
    """Context carried by one user turn.

    Canvas references and uploaded files deliberately share the outer list so
    retry/idempotency/checkpoint code has one stable protocol.  File payloads
    live in the chat VFS; only durable metadata is copied into the message.
    """

    type: Literal["node", "edge", "ref", "file", "image", "video"]
    id: str | None = None
    source: str | None = None
    target: str | None = None
    name: str | None = None
    path: str | None = None
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_variant(self) -> "Attachment":
        if self.type in {"file", "image", "video"}:
            if not self.name or not self.path:
                raise ValueError("file attachments require name and path")
            if not self.path.startswith(("/data/", "/mount/")):
                raise ValueError("file attachment path must be a VFS path")
            if any(part in {"", ".", ".."} for part in self.path.split("/")[1:]):
                raise ValueError("file attachment path contains an invalid segment")
        return self


class AgentSettings(BaseModel):
    """Per-turn "Agent settings" override (the sidebar gear). All optional —
    omitting the block (or every field) keeps the platform-default agent LLM.

    ``model_id`` is an opaque id returned by the bound runtime's capabilities
    endpoint. The API resolves it to SDK-specific config; the client never
    sends credentials or provider secrets. Hyperparameters are forwarded to
    the selected runtime when supported."""
    model_id: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    reasoning_effort: str | None = None

    @field_validator("model_id", "reasoning_effort")
    @classmethod
    def validate_non_empty_runtime_choice(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("runtime choices must be non-empty strings")
        return normalized


class AgentRuntimeOptionOut(BaseModel):
    id: str
    label: str
    description: str = ""


class AgentRuntimeSettingsOut(BaseModel):
    default_runtime_type: str = "codex"
    codex_managed_profile_id: str | None = None
    preferred_timezone: str | None = None
    codex_managed_profiles: list[dict[str, str | int]] = Field(default_factory=list)
    codex_auth_methods: list[str] = Field(default_factory=list)
    available_runtime_types: list[str] = Field(
        default_factory=lambda: ["codex"]
    )
    runtime_options: list[AgentRuntimeOptionOut] = Field(default_factory=list)


class AgentRuntimeSettingsUpdate(BaseModel):
    default_runtime_type: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class UserTimezoneUpdate(BaseModel):
    preferred_timezone: str = Field(min_length=1, max_length=128)

    @field_validator("preferred_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid IANA timezone") from exc
        return normalized


class CodexManagedProfileUpdate(BaseModel):
    profile_id: str = Field(min_length=1, max_length=100)


class ChatRuntimeBindingOut(BaseModel):
    runtime_type: str | None = None
    runtime_version: int = 1
    runtime_model_id: str | None = None
    runtime_connection_id: str | None = None
    runtime_agent_settings: AgentSettings | None = None


class HitlContinueControl(BaseModel):
    """Platform control that starts a new Human Turn without a visible bubble."""

    type: Literal["hitl_continue"]
    version: Literal[1] = 1
    hitl_request_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=128)
    action: Literal["continue"] = "continue"


class BackgroundResultsControl(BaseModel):
    """Backend-originated batch of durable background job results."""

    type: Literal["background_results"]
    version: Literal[1] = 1
    batch_id: str = Field(min_length=1, max_length=128)
    job_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("job_ids")
    @classmethod
    def validate_job_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if not value or len(value) > 128:
                raise ValueError("invalid background job id")
            if value not in seen:
                normalized.append(value)
                seen.add(value)
        if not normalized:
            raise ValueError("background result batch cannot be empty")
        return normalized


class MessagePostBody(BaseModel):
    role: Literal["user"] = "user"
    content: str = ""
    # Control messages use the ordinary Turn endpoint and Runtime history, but
    # are hidden from the product transcript. Their model-facing content is
    # resolved from durable HITL state by the backend, never supplied by UI text.
    control: HitlContinueControl | BackgroundResultsControl | None = None
    # Client-generated idempotency key. A retried POST with the same key must
    # resolve to the same durable Agent Run instead of duplicating the message.
    client_request_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    # Browser mode adds the extension-backed toolset. Workflow construction
    # uses the ordinary command registry (`/workflow`).
    mode: Literal["chat", "browser"] = "chat"
    # Reserved per-turn authorization policy. The UI currently omits this
    # control and defaults pre-tool execution to automatic approval; the enum
    # remains in the API as a future Runtime-neutral approval extension seam.
    approval_mode: Literal["agent", "always_ask", "always_allow"] = "always_allow"
    # Agent settings gear: optional per-turn LLM credential + hyperparam override.
    agent_settings: AgentSettings | None = None
    # Where the chat lives. `/browser` is a SIDE-PANEL-only command (browser
    # control runs in the extension, not the main app). The side-panel embed
    # sends "sidepanel"; the main app defaults to "main". The route uses this to
    # gate `/browser`: in the main app it is refused with a NOTICE (no agent
    # turn), in the side panel it activates browser mode normally.
    surface: Literal["main", "sidepanel"] = "main"
    # Product surface that invokes the agent. This is distinct from `surface`
    # above, which is the client container ("main" app vs extension sidepanel).
    # `agent_surface` controls prompt/tool assembly boundaries.
    agent_surface: Literal["chat", "browser"] = "chat"
    # Complete custom-MCP selection rendered by the composer for this Turn.
    # Platform workflow/browser MCPs are activated only through commands and
    # never appear in this list.
    mcp_server_ids: list[str] = Field(default_factory=list)
    chat_config_revision: int = Field(default=0, ge=0)
    # Browser projection of the user's platform timezone.  It is consulted
    # only while the first Runtime Turn atomically fixes the Chat clock;
    # resumes never replace an existing clock.
    timezone: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("timezone")
    @classmethod
    def validate_turn_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid IANA timezone") from exc
        return normalized

    @model_validator(mode="after")
    def validate_message_kind(self) -> "MessagePostBody":
        if self.control is None:
            if not self.content.strip() and not self.attachments:
                raise ValueError("a text message requires content or attachments")
            return self
        if self.content or self.attachments:
            raise ValueError("a control message cannot include text or attachments")
        if self.mode != "chat":
            raise ValueError("a control message must use chat mode")
        return self

    @field_validator("mcp_server_ids")
    @classmethod
    def validate_mcp_server_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen = set()
        for raw in values:
            try:
                value = str(uuid.UUID(raw))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid MCP server id: {raw!r}") from exc
            if value not in seen:
                normalized.append(value)
                seen.add(value)
        return normalized


class HistoryMessage(BaseModel):
    id: str | None = None
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    attachments: list[Attachment] = Field(default_factory=list)
    ts: float | None = None
    tool_calls: list[dict] | None = None
    # For a ``role: "tool"`` message, the id of the tool_call it answers. The
    # frontend folds the result into that call by this id; without it a reloaded
    # tool call is stuck "waiting for result" with no output (the live stream
    # carries it, but the history read must too).
    tool_call_id: str | None = None
    # Structured tool envelope for frontend rendering/debug. The model-facing
    # text remains `content`; this is the ToolMessage.artifact side channel.
    artifact: dict | None = None
    invocation: dict | None = None
    activity: dict | None = None
    # Populated only when the history read is called with ?debug=true — per-message
    # debug meta (approx_tokens, content_type/path for tool outputs, frozen/aged_form
    # if the lifecycle middleware degraded it). Always None in the normal read.
    meta: dict | None = None


class ActiveAgentRun(BaseModel):
    run_id: str
    chat_id: str
    status: Literal[
        "running", "waiting_approval", "cancel_requested",
        "completed", "cancelled", "failed",
    ]
    last_event_id: int = 0
    created_at: datetime
    input_message_id: str | None = None
    # Canonical active-Turn user projection. A fresh frontend loads history
    # only up to ``before_turn_id`` and rebuilds the active tail from the Run;
    # without this record, refresh would restore tool/HITL events but omit the
    # user bubble that initiated them.
    input_message: HistoryMessage | None = None
    pending_hitl: list[HitlRequestOut] = Field(default_factory=list)


class BrowserBindingOut(BaseModel):
    chat_id: str
    status: Literal["inactive", "attaching", "attached", "lost"]
    browser_lost_at: str | None = None


class HitlAction(BaseModel):
    id: str
    label: str
    variant: str = "primary"


class HitlRequestOut(BaseModel):
    hitl_request_id: str
    chat_id: str
    run_id: str | None = None
    artifact_id: str | None = None
    hitl_type: str
    status: str
    title: str = ""
    prompt_text: str = ""
    ui_payload_json: dict = Field(default_factory=dict)
    ui_projection_event_json: dict = Field(default_factory=dict)
    decision_payload_json: dict = Field(default_factory=dict)
    interaction_result_json: dict = Field(default_factory=dict)
    is_interacted: bool = False
    created_at: datetime
    resolved_at: datetime | None = None
    # Present on decision responses. Only the request that performed the
    # pending -> terminal transition may trigger a follow-up Human Turn.
    decision_applied: bool | None = None


class HitlDecisionBody(BaseModel):
    decision: Literal[
        "approve", "approved", "deny", "denied",
        "submit", "submitted", "cancel", "cancelled",
    ]
    decision_payload: dict = Field(default_factory=dict)
    interaction_result: dict = Field(default_factory=dict)


class InteractiveArtifactStateBody(BaseModel):
    """Durable draft projected from standard named controls in sandboxed HTML."""

    state: dict = Field(default_factory=dict)


class InteractiveArtifactResultFileBody(BaseModel):
    """Text result produced by an interactive artifact before HITL resolution."""

    content: str
    path: str | None = None
    content_type: Literal[
        "application/json", "text/csv", "text/plain", "text/tab-separated-values",
    ] = "application/json"
