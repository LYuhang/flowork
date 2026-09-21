"""Transport-neutral registry and invocation service for Platform tools."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from mcp import types

from vibecanvas_api.services.platform_mcp.tool_runtime import AgentContext, ToolRuntime
from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.auth.live_identity import (
    LiveIdentityError,
    resolve_live_authorization_identity,
)
from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
    scope_authz_service,
)
from vibecanvas_api.authorization.openfga_client import OpenFgaUnavailableError
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.services.chat_workspace import chat_workspace_scope_id
from vibecanvas_api.services.platform_mcp.authorization import (
    platform_mcp_tool_action,
    platform_resource_tool_action,
    prepare_platform_tool,
)
from vibecanvas_api.services.platform_mcp.build_tools import BUILD_TOOLS
from vibecanvas_api.services.platform_mcp.build_tools.workflow_context import (
    create_workflow,
    set_workflow,
)
from vibecanvas_api.services.platform_mcp.capability import (
    PlatformMcpCapability,
    verify_platform_mcp_capability,
)
from vibecanvas_api.services.platform_mcp.catalog import PLATFORM_MCP_METADATA
from vibecanvas_api.services.platform_mcp.config_tools import CONFIG_TOOLS
from vibecanvas_api.services.platform_mcp.interactive_tools import INTERACTIVE_TOOLS
from vibecanvas_api.services.platform_mcp.resource_tools import (
    DEPLOYMENT_MCP_TOOLS,
    KNOWLEDGE_MCP_TOOLS,
    TASK_MCP_TOOLS,
)
from vibecanvas_api.services.platform_mcp.run_tools import RUN_TOOLS
from vibecanvas_api.services.platform_mcp.workflow_tools import WORKFLOW_MCP_TOOLS
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.sync_repo import SyncWorkflowRepo
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.vfs_store import PostgresVfsStore


_OPENFGA_CLIENT = None


def set_platform_mcp_openfga_client(client) -> None:
    """Publish the API lifespan's shared authorization client."""
    global _OPENFGA_CLIENT
    _OPENFGA_CLIENT = client


async def _context_for(capability: PlatformMcpCapability) -> AgentContext:
    """Rebuild ephemeral tool context from durable backend state."""
    identity = await _live_platform_identity(capability)
    async with session_scope(tenant_id=capability.tenant_id) as session:
        service = authz_service_for_session(
            session=session,
            organization_id=capability.organization_id,
            openfga_client=_OPENFGA_CLIENT,
        )
        service = scope_authz_service(
            service,
            session=session,
            auth=identity,
            audit_uses=True,
        )
        try:
            chat_decision = await service.check(
                PrincipalRef(PrincipalType.USER, capability.user_id),
                Action.EXECUTE,
                ResourceRef(
                    ResourceType.CHAT,
                    capability.chat_id,
                    capability.organization_id,
                ),
                AuthzRequestContext(
                    active_organization_id=capability.organization_id,
                    request_id=f"platform-mcp:{capability.turn_id}",
                    session_id=capability.session_id,
                    session_generation=capability.session_generation,
                    membership_id=identity.membership_id,
                    membership_role=identity.membership_role,
                    membership_status=identity.membership_status,
                    authentication_strength=identity.authentication_strength,
                    consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
                ),
            )
        except OpenFgaUnavailableError as exc:
            raise PermissionError(
                "Platform MCP authorization is temporarily unavailable"
            ) from exc
        if not chat_decision.allowed:
            raise PermissionError("Platform MCP Chat access has been revoked")
        run = await AgentRunsRepo(session).get_for_chat(
            capability.chat_id,
            capability.turn_id,
            creator_user_id=capability.user_id,
        )
        if run is None or run.status != "running":
            raise PermissionError("Platform MCP capability is not bound to an active run")
        binding = await ChatRepo(
            session, capability.user_id
        ).get_platform_context_binding(capability.chat_id)
        if binding is None:
            raise PermissionError("Platform MCP capability is not bound to an active Chat")
        if binding.get("runtime_session_id") != capability.runtime_session_id:
            raise PermissionError(
                "Platform MCP capability Runtime binding is stale"
            )
        expected_workspace_scope_id = chat_workspace_scope_id(
            capability.chat_id
        )
        if expected_workspace_scope_id != capability.workspace_scope_id:
            raise PermissionError(
                "Platform MCP capability workspace does not match its Chat"
            )
        current_workflow_id = binding["current_workflow_id"]

    workflow_repo = SyncWorkflowRepo(capability.user_id)

    return AgentContext(
        # Never preload selected workflow content before the concrete tool
        # action is authorized. _invoke populates it after the action check.
        workflow={},
        repo=workflow_repo,
        vfs=PostgresVfsStore(),
        username=capability.user_id,
        wf_id=capability.workspace_scope_id,
        tenant_id=capability.tenant_id,
        chat_id=capability.chat_id,
        turn_id=capability.turn_id,
        authorization_client=_OPENFGA_CLIENT,
        authorization_session_id=capability.session_id,
        authorization_membership_id=identity.membership_id,
        authorization_membership_role=identity.membership_role,
        authorization_membership_status=identity.membership_status,
        authorization_session_generation=capability.session_generation,
        authorization_generation=capability.authorization_generation,
        authorization_authentication_strength=identity.authentication_strength,
        authorization_session_audience=identity.session_audience,
        authorization_privileged_access_request_id=(
            identity.privileged_access_request_id
        ),
        authorization_privileged_resource_type=identity.privileged_resource_type,
        authorization_privileged_resource_id=identity.privileged_resource_id,
        authorization_privileged_actions=tuple(identity.privileged_actions),
        authorization_privileged_expires_at=identity.privileged_expires_at,
        runtime_session_id=capability.runtime_session_id,
        surface="chat",
        approval_mode=capability.approval_mode,
        runtime_location="platform_mcp",
        current_workflow_id=current_workflow_id,
    )


async def _live_platform_identity(
    capability: PlatformMcpCapability,
) -> AuthContext:
    """Fence a capability to the still-live browser Session and membership."""
    from vibecanvas_api.services.agent_runtime.model_capability import (
        authorization_model_generation,
    )

    expected_generation = authorization_model_generation(
        model_id=config.openfga_authorization_model_id,
    )
    if capability.authorization_generation != expected_generation:
        raise PermissionError("Platform MCP authorization generation is stale")

    async with session_scope() as identity_session:
        try:
            return await resolve_live_authorization_identity(
                identity_session,
                session_id=capability.session_id,
                user_id=capability.user_id,
                organization_id=capability.organization_id,
                session_generation=capability.session_generation,
                membership_id=capability.membership_id,
            )
        except LiveIdentityError as exc:
            raise PermissionError(
                "Platform MCP browser identity has been revoked"
            ) from exc


def _input_schema(tool: Any) -> dict[str, Any]:
    properties = dict(getattr(tool, "args", {}) or {})
    required = [
        name
        for name, schema in properties.items()
        if isinstance(schema, dict) and "default" not in schema
    ]
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def _tool_error_result(
    *,
    code: str,
    message: str,
    tool_name: str,
    json_pointer: str | None = None,
) -> types.CallToolResult:
    """Return an MCP-native, model-correctable tool error.

    Business/tool input errors are results with ``isError=true`` per the MCP
    specification.  They are not transport failures and never manufacture a
    downstream Diagram reference.
    """
    error = {
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            **({"json_pointer": json_pointer} if json_pointer else {}),
        },
        "retry": {
            "tool": tool_name,
            "instruction": (
                "Correct only the rejected argument using this tool's "
                "published inputSchema and exact previously returned values."
            ),
        },
    }
    return types.CallToolResult(
        content=[types.TextContent(
            type="text",
            text=json.dumps(error, ensure_ascii=False),
        )],
        isError=True,
    )


def _output_schema(tool: Any) -> dict[str, Any] | None:
    """Existing HTTP platform tools use their established text envelope."""
    del tool
    return None


def _annotations(name: str) -> types.ToolAnnotations:
    read_prefixes = (
        "list_",
        "get_",
        "check_",
    )
    read_only = name.startswith(read_prefixes) or name in {
        "task_list",
        "task_get",
        "task_collect_diagnostics",
        "deployment_list",
        "deployment_get",
        "deployment_collect_diagnostics",
        "knowledge_list",
        "knowledge_get",
        "knowledge_search",
    }
    # Hints improve third-party client UX; security decisions still use the
    # backend-owned approval policy and concrete CallTool arguments.
    destructive = name in {
        "task_create_scheduled_run",
        "task_update_scheduled_run",
        "task_delete_scheduled_run",
        "task_cancel",
        "task_resume",
        "deployment_create",
        "deployment_update",
        "deployment_delete",
        "knowledge_create",
        "knowledge_update",
        "knowledge_delete",
    }
    return types.ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=read_only,
        openWorldHint=name == "render_url_preview",
    )


def _required_tool_actions(server: str, tool_name: str) -> frozenset[str]:
    """Map a registered tool to the signed action ceiling it consumes."""
    required = {"chat:execute", "platform_mcp:call"}
    if server in {"workflow", "build"}:
        action = platform_mcp_tool_action(tool_name)
        if tool_name == "create_workflow":
            required.add("organization:create")
        else:
            required.add(f"workflow:{action.value}")
        if server == "build" and tool_name in {
            "check_workflow",
            "update_canvas",
            "run_workflow",
            "node_execute",
            "batch_execute",
        }:
            required.add("vfs_path:view")
        if server == "build" and tool_name in {
            "update_canvas",
            "run_workflow",
            "node_execute",
            "batch_execute",
        }:
            required.add("vfs_path:update")
    elif server in {"task", "deployment", "knowledge"}:
        action = platform_resource_tool_action(server, tool_name)
        resource_type = {
            "task": "task",
            "deployment": "deployment",
            "knowledge": "knowledge_base",
        }[server]
        required.add(f"{resource_type}:{action.value}")
        if tool_name == "task_create_scheduled_run":
            required.update({"organization:create", "workflow:use"})
        elif tool_name == "deployment_create":
            required.update({"organization:create", "workflow:deploy"})
        elif tool_name == "knowledge_create":
            required.add("organization:create")
    elif server == "config" and tool_name == "get_config":
        required.update(
            {"platform_catalog:view", "llm_credential:view_metadata"}
        )
    elif server == "interactive" and tool_name in {
        "render_interactive",
        "render_url_preview",
    }:
        required.update({
            "interactive_artifact:create",
        })
        if tool_name == "render_interactive":
            required.add("vfs_path:view")
    else:
        raise PermissionError(
            f"Platform MCP tool {server}/{tool_name} has no capability policy"
        )
    return frozenset(required)


def _require_tool_capability(
    capability: PlatformMcpCapability,
    *,
    server: str,
    tool_name: str,
) -> None:
    required = _required_tool_actions(server, tool_name)
    if not required.issubset(capability.actions):
        raise PermissionError(
            f"Platform MCP capability does not permit {server}/{tool_name}"
        )


def _platform_events(context: AgentContext, tool_name: str) -> list[dict[str, Any]]:
    pending = context.pending_vibe
    if pending:
        workflow = pending.get("new_workflow") or {}
        meta = workflow.get("__meta__") or {}
        return [
            {
                "type": "VIBE_ACTION",
                "payload": {
                    "updates": list(pending.get("updates") or []),
                    "apply_auto_layout": bool(pending.get("apply_auto_layout")),
                    "workflow_id": meta.get("workflow_id"),
                    "workflow_version": meta.get("workflow_version"),
                    "workflow_subversion": meta.get("workflow_subversion"),
                },
            },
            {"type": "META_SYNC", "payload": {"meta": meta}},
        ]
    if tool_name in {"new_version", "set_workflow", "create_workflow"}:
        return [
            {
                "type": "META_SYNC",
                "payload": {"meta": (context.workflow or {}).get("__meta__", {})},
            }
        ]
    return []


async def invoke_platform_mcp_tool_with_capability(
    tool: Any,
    arguments: dict[str, Any],
    server: str,
    capability: PlatformMcpCapability,
):
    """Invoke one Platform tool after the transport authenticated its caller."""
    _require_tool_capability(
        capability,
        server=server,
        tool_name=str(tool.name),
    )
    tenant_token = current_sync_tenant_id.set(capability.tenant_id)
    try:
        context = await _context_for(capability)
        runtime = ToolRuntime(
            state={},
            context=context,
            config={"configurable": {"thread_id": capability.chat_id}},
            stream_writer=lambda _chunk: None,
            tool_call_id=None,
            store=None,
            tools=[],
        )
        await prepare_platform_tool(
            context,
            server=server,
            tool_name=str(tool.name),
            arguments=arguments,
        )
        coroutine = getattr(tool, "coroutine", None)
        if coroutine is None:
            raise RuntimeError(f"platform tool {tool.name} is not asynchronous")
        result = await coroutine(runtime=runtime, **arguments)
        if not isinstance(result, tuple) or len(result) != 2:
            raise RuntimeError(f"platform tool {tool.name} returned an invalid result")
        content, artifact = result
        if not isinstance(artifact, dict):
            raise TypeError(f"platform tool {tool.name} returned no structured artifact")
        if artifact.get("status") == "error":
            error = artifact.get("error")
            error = error if isinstance(error, dict) else {}
            return _tool_error_result(
                code=str(error.get("code") or "platform_tool_failed"),
                message=str(
                    error.get("message") or content or "Platform tool failed"
                ),
                tool_name=str(tool.name),
            )
        events = _platform_events(context, str(tool.name))
        if events:
            artifact = dict(artifact)
            artifact["meta"] = {
                **dict(artifact.get("meta") or {}),
                "platform_events": events,
            }
        blocks: list[types.ContentBlock] = [
            types.TextContent(type="text", text=str(content))
        ]
        for item in list((artifact.get("meta") or {}).get("mcp_content") or []):
            if item.get("type") == "image":
                blocks.append(types.ImageContent(
                    type="image",
                    data=str(item.get("data") or ""),
                    mimeType=str(item.get("mime_type") or "image/png"),
                ))
        structured = artifact.get("structured_content")
        if structured is None:
            structured = artifact
        return blocks, structured
    finally:
        # MCP requests are long-lived async tasks. Never leak one tenant's sync
        # repository context into a later request reusing the same worker task.
        current_sync_tenant_id.reset(tenant_token)


def _validated_tool_call(
    *,
    server: str,
    tool_map: dict[str, Any],
    name: str,
    arguments: dict[str, Any],
) -> tuple[Any | None, types.CallToolResult | None]:
    tool = tool_map.get(name)
    if tool is None:
        raise ValueError(f"unknown {server} tool: {name}")
    input_schema = _input_schema(tool)
    errors = sorted(
        Draft202012Validator(input_schema).iter_errors(arguments),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if not errors:
        return tool, None
    error = errors[0]
    pointer = "/" + "/".join(
        str(item).replace("~", "~0").replace("/", "~1")
        for item in error.absolute_path
    )
    return None, _tool_error_result(
        code="invalid_tool_arguments",
        message=error.message,
        tool_name=name,
        json_pointer=pointer if pointer != "/" else None,
    )


_PLATFORM_MCP_TOOLSETS: dict[str, tuple[Any, ...]] = {
    "config": tuple(CONFIG_TOOLS),
    "interactive": tuple(INTERACTIVE_TOOLS),
    "workflow": tuple(WORKFLOW_MCP_TOOLS),
    "task": tuple(TASK_MCP_TOOLS),
    "deployment": tuple(DEPLOYMENT_MCP_TOOLS),
    "knowledge": tuple(KNOWLEDGE_MCP_TOOLS),
    "build": (set_workflow, create_workflow, *BUILD_TOOLS, *RUN_TOOLS),
}


def platform_mcp_tool_manifest(server: str) -> list[types.Tool]:
    """Return the canonical tool manifest without exposing a transport URL."""
    try:
        tools = _PLATFORM_MCP_TOOLSETS[server]
    except KeyError as exc:
        raise ValueError(f"unknown platform MCP server: {server}") from exc
    return [
        types.Tool(
            name=str(tool.name),
            description=str(getattr(tool, "description", "") or ""),
            inputSchema=_input_schema(tool),
            outputSchema=_output_schema(tool),
            annotations=_annotations(str(tool.name)),
        )
        for tool in tools
    ]


def platform_mcp_catalog_entry(server: str) -> dict[str, Any]:
    """Return one secret-free internal contract projection for diagnostics."""
    try:
        metadata = PLATFORM_MCP_METADATA[server]
    except KeyError as exc:
        raise ValueError(f"unknown platform MCP server: {server}") from exc
    return {
        **metadata,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
                "output_schema": tool.outputSchema,
                "annotations": (
                    tool.annotations.model_dump(
                        by_alias=True,
                        exclude_none=True,
                    )
                    if tool.annotations is not None
                    else {}
                ),
            }
            for tool in platform_mcp_tool_manifest(server)
        ],
    }


async def invoke_platform_mcp_tool(
    *,
    server: str,
    tool_name: str,
    arguments: dict[str, Any],
    capability_token: str,
) -> types.CallToolResult | tuple[list[types.ContentBlock], dict[str, Any]]:
    """Host Gateway entrypoint shared by private RPC and legacy HTTP adapters.

    The caller supplies a short-lived backend-minted capability. No FastMCP
    request context, HTTP authority, or model-visible Platform URL is required.
    """
    capability = verify_platform_mcp_capability(
        capability_token,
        secret=config.signing_secret,
        server=server,
    )
    if capability is None:
        raise PermissionError("invalid or expired Platform MCP capability")
    try:
        tools = _PLATFORM_MCP_TOOLSETS[server]
    except KeyError as exc:
        raise ValueError(f"unknown platform MCP server: {server}") from exc
    tool, error_result = _validated_tool_call(
        server=server,
        tool_map={str(item.name): item for item in tools},
        name=tool_name,
        arguments=dict(arguments),
    )
    if error_result is not None:
        return error_result
    assert tool is not None
    return await invoke_platform_mcp_tool_with_capability(
        tool,
        dict(arguments),
        server,
        capability,
    )




def platform_mcp_tool_implementations(server: str) -> tuple[Any, ...]:
    """Return immutable canonical tool implementations for one capability."""
    try:
        return _PLATFORM_MCP_TOOLSETS[server]
    except KeyError as exc:
        raise ValueError(f"unknown platform MCP server: {server}") from exc


__all__ = [
    "invoke_platform_mcp_tool",
    "invoke_platform_mcp_tool_with_capability",
    "platform_mcp_catalog_entry",
    "platform_mcp_tool_implementations",
    "platform_mcp_tool_manifest",
    "set_platform_mcp_openfga_client",
]
