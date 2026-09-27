"""Transport-neutral registry and invocation service for Platform tools."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from mcp import types

from vibecanvas_api.agents.tool_runtime import ToolRuntime
from vibecanvas_api.config import config
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.capability import (
    AgentCapability,
    verify_agent_capability,
)
from vibecanvas_api.services.platform_mcp.catalog import PLATFORM_MCP_METADATA
from vibecanvas_api.services.platform_mcp.interactive_tools import (
    INTERACTIVE_TOOLS,
)
from vibecanvas_api.storage.sync_session import current_sync_tenant_id


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
    arguments_model = getattr(tool, "tool_call_schema", None)
    if arguments_model is not None and hasattr(arguments_model, "model_json_schema"):
        definitions = arguments_model.model_json_schema().get("$defs")
        if definitions:
            result["$defs"] = definitions
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
                "The workflow is unavailable or access was revoked. Do not repeat the same preview, recreate the resource, or change saved versions to bypass this result. Verify an accessible target or report the blocker."
                if code == "workflow_unavailable" else
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
    # Both tools persist an artifact; choices also waits for user input.
    return types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=name == "render_preview",
    )


def _required_tool_actions(server: str, tool_name: str) -> frozenset[str]:
    """Authorize only the published render surface; no retired business tools."""
    if server != "interactive" or tool_name not in {"render_preview", "render_choices"}:
        raise PermissionError(f"Platform MCP tool {server}/{tool_name} has no capability policy")
    required = {"chat:execute", "platform_mcp:call", "interactive_artifact:create"}
    if tool_name == "render_preview":
        # Concrete file/workflow access is checked again when resolving the source.
        required.update({"vfs_path:view", "workflow:view", "workflow:use"})
    return frozenset(required)


def _require_tool_capability(
    capability: AgentCapability,
    *,
    server: str,
    tool_name: str,
) -> None:
    required = _required_tool_actions(server, tool_name)
    if capability.server != server or not required.issubset(capability.actions):
        raise PermissionError(
            f"Platform MCP capability does not permit {server}/{tool_name}"
        )


async def invoke_platform_mcp_tool_with_capability(
    tool: Any,
    arguments: dict[str, Any],
    server: str,
    capability: AgentCapability,
):
    """Invoke one Platform tool after the transport authenticated its caller."""
    _require_tool_capability(
        capability,
        server=server,
        tool_name=str(tool.name),
    )
    tenant_token = current_sync_tenant_id.set(capability.tenant_id)
    try:
        context = await agent_context.resolve_context(capability)
        runtime = ToolRuntime(
            state={},
            context=context,
            config={"configurable": {"thread_id": capability.chat_id}},
            stream_writer=lambda _chunk: None,
            tool_call_id=None,
            store=None,
            tools=[],
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
    "interactive": tuple(INTERACTIVE_TOOLS),
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
    """Host Gateway entrypoint for the two published render tools.

    The caller supplies a short-lived backend-minted capability. No FastMCP
    request context, HTTP authority, or model-visible Platform URL is required.
    """
    capability = verify_agent_capability(
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
        tool_map={
            str(item.name): item
            for item in tools
        },
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
]
