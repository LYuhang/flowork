"""Small, SDK-neutral tool definitions for Flowork's Platform MCP surface.

The Platform MCP server only needs a callable, a description, and a JSON
schema.  Keeping those primitives here avoids pulling an entire Agent SDK into
the API process merely to describe and invoke MCP tools.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

from pydantic import BaseModel, Field, PrivateAttr, create_model


class AgentContext(BaseModel):
    """Ephemeral, host-owned context shared by Platform MCP tools."""

    workflow: dict = Field(default_factory=dict)
    pending_vibe: dict | None = None
    workflow_dirty: bool = False
    repo: Any = None
    vfs: Any = None
    vfs_run: Any = None
    username: str = ""
    wf_id: str = ""
    tenant_id: str | None = None
    chat_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    authorization_client: Any = Field(default=None, exclude=True)
    authorization_session_id: str = Field(default="", exclude=True)
    authorization_membership_id: str = Field(default="", exclude=True)
    authorization_membership_role: str = Field(default="", exclude=True)
    authorization_membership_status: str = Field(default="", exclude=True)
    authorization_session_generation: int = Field(default=0, exclude=True)
    authorization_generation: str = Field(default="", exclude=True)
    authorization_authentication_strength: str = Field(default="", exclude=True)
    authorization_session_audience: str = Field(default="web", exclude=True)
    authorization_privileged_access_request_id: str = Field(default="", exclude=True)
    authorization_privileged_resource_type: str = Field(default="", exclude=True)
    authorization_privileged_resource_id: str = Field(default="", exclude=True)
    authorization_privileged_actions: tuple[str, ...] = Field(default=(), exclude=True)
    authorization_privileged_expires_at: Any = Field(default=None, exclude=True)
    runtime_session_id: str = Field(default="", exclude=True)
    surface: str = "chat"
    approval_mode: str = "agent"
    runtime_location: str = "host"
    tool_approval_decisions: dict[str, str] = Field(default_factory=dict)
    interactive_artifact_refs: dict[str, dict] = Field(default_factory=dict)
    active_commands: list[str] = Field(default_factory=list)
    available_commands: list[str] = Field(default_factory=list)
    current_workflow_id: str | None = None
    run_id: str = ""
    context_manifest: dict[str, Any] = Field(default_factory=dict)
    runtime_mcp_catalog: list[dict] = Field(default_factory=list)
    runtime_skill_catalog: list[dict] = Field(default_factory=list)
    todo_items: list[dict] = Field(default_factory=list)
    pending_images: list = Field(default_factory=list)
    # Workflow SubAgentNode uses the same SDK-neutral context object. These
    # fields are inert for Platform MCP and populated only inside the workflow
    # sandbox when that node is executed.
    agent_cfg: Any = None
    stop_event: Any = None
    staged_subagent_output: dict[str, Any] | None = None
    _attached_session: Any = PrivateAttr(default=None)

    model_config = {"arbitrary_types_allowed": True}

    async def sandbox_session(self):
        """Borrow the active Chat sandbox without creating a second Runtime."""
        if not self.tenant_id or not self.wf_id:
            raise ValueError(
                "AgentContext.sandbox_session() requires both tenant_id and wf_id"
            )
        if self._attached_session is not None:
            return self._attached_session
        if os.environ.get("VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX") == "1":
            from vibecanvas_api.services.agent_runtime.local_session import (
                LocalAgentRuntimeSession,
            )

            self._attached_session = LocalAgentRuntimeSession()
            return self._attached_session

        from vibecanvas_api.services.sandbox.manager import get_sandbox_manager

        manager = get_sandbox_manager()
        if self.runtime_location == "platform_mcp":
            session = await manager.get_loaded_session(self.tenant_id, self.wf_id)
            if session is None:
                raise RuntimeError(
                    "Platform MCP requires the active Runtime sandbox session"
                )
        else:
            session = await manager.get_session(
                self.tenant_id,
                self.wf_id,
                self.username or None,
                expose_run=True,
            )
        self._attached_session = session
        return session


class ToolRuntime:
    """Invocation context passed to Platform MCP tool implementations."""

    def __init__(
        self,
        *,
        state: dict,
        context: AgentContext,
        config: dict,
        stream_writer: Callable[[Any], None],
        tool_call_id: str | None,
        store: Any,
        tools: list,
    ) -> None:
        self.state = state
        self.context = context
        self.config = config
        self.stream_writer = stream_writer
        self.tool_call_id = tool_call_id
        self.store = store
        self.tools = tools


@dataclass(frozen=True, slots=True)
class PlatformTool:
    name: str
    description: str
    args: dict[str, Any]
    tool_call_schema: type[BaseModel]
    coroutine: Callable[..., Any]


def tool(tool_name: str | None = None, *, response_format: str | None = None):
    """Describe an async Platform MCP tool without an Agent SDK dependency."""
    del response_format

    def decorate(function: Callable[..., Any]) -> PlatformTool:
        signature = inspect.signature(function)
        hints = get_type_hints(function)
        fields: dict[str, tuple[Any, Any]] = {}
        for parameter_name, parameter in signature.parameters.items():
            if parameter_name == "runtime":
                continue
            annotation = hints.get(parameter_name, Any)
            default = (
                ...
                if parameter.default is inspect.Parameter.empty
                else parameter.default
            )
            fields[parameter_name] = (annotation, default)
        arguments_model = create_model(
            f"{function.__name__.title().replace('_', '')}Arguments",
            **fields,
        )
        schema = arguments_model.model_json_schema()
        return PlatformTool(
            name=tool_name or function.__name__,
            description=inspect.getdoc(function) or "",
            args=dict(schema.get("properties") or {}),
            tool_call_schema=arguments_model,
            coroutine=function,
        )

    return decorate


__all__ = ["AgentContext", "PlatformTool", "ToolRuntime", "tool"]
