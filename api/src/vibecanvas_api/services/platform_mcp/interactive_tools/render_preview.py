"""Unified Platform MCP entrypoint for durable file, URL and workflow previews."""
from __future__ import annotations

from typing import Any, Literal

from vibecanvas_api.agents.tool_runtime import ToolRuntime, tool
from vibecanvas_api.agents.tools.decorator import ToolError, tool_error_boundary
from vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact import (
    _render_view,
)


@tool(response_format="content_and_artifact")
@tool_error_boundary(tool="render_preview")
async def render_preview(
    type: Literal["file", "url", "workflow"],
    source: str = "",
    title: str = "",
    file_type: str = "auto",
    description: str = "",
    version: str = "",
    *,
    runtime: ToolRuntime,
) -> tuple[str, dict[str, Any]]:
    """Publish a file, web address, or saved workflow as a durable Preview card.

    Use ``type="file", source="/data/report.pdf"`` for an existing absolute
    local file path, or ``type="url", source="https://example.com"`` for an
    HTTP(S) web address. Save generated HTML to an .html file and use file mode.
    HTML files are read-only previews: external images/media/scripts are blocked.
    Save images to the sandbox and use relative paths or absolute sandbox paths
    such as /data/photo.jpg; data: images also work. Keep website URLs as source
    hyperlinks. Do not use external image URLs or temporary signed URLs in a report.
    Use type="workflow", source="<workflow id>", version="v1.sv3" to preview
    a saved graph. source is required; omitting version resolves global HEAD
    and pins that exact version in the card. Pass the version returned by your
    CLI command to preview the branch you edited. No Chat binding is consulted.
    Opening it rechecks access. This tool never saves, uploads or runs a graph;
    use flowork-cli workflow upload --workflow_id ID --major vN --file PATH first to save local changes.
    Pass flat arguments, not a nested view object.

    ``title`` and ``description`` are optional. ``file_type`` defaults to
    ``auto``; override it only for ambiguous files, never for URLs or workflows.
    This tool only displays content and never waits. Use render_choices to ask
    the user to choose and wait for their confirmation inside that tool call.
    Returns a persisted interactive artifact. Main chat can open all three kinds
    in its side Preview pane; compact clients render inline. URL previews
    respect the destination's embedding restrictions and do not control a browser.
    """
    if type not in {"file", "url", "workflow"}:
        raise ToolError("invalid_interactive_input", 'type must be "file", "url", or "workflow".')
    if type != "workflow" and version:
        raise ToolError("invalid_interactive_input", 'version is only supported for type="workflow".')
    if type != "file" and file_type != "auto":
        raise ToolError(
            "invalid_interactive_input",
            'file_type is only supported for type="file".',
        )
    if type == "workflow":
        from vibecanvas_api.services.agent_resources.workflow_transfer import read_workflow_snapshot

        snapshot = await read_workflow_snapshot(runtime.context, workflow_id=source.strip(), version=version.strip())
        return await _render_view(
            type="workflow_preview", workflow_id=snapshot["id"], version=snapshot["version"],
            title=title or snapshot["name"], description=description,
            publisher_tool="render_preview", runtime=runtime,
        )
    return await _render_view(
        type="file_preview" if type == "file" else "url_preview",
        path=source if type == "file" else "",
        url=source if type == "url" else "",
        title=title,
        file_type=file_type,
        description=description,
        publisher_tool="render_preview",
        runtime=runtime,
    )
