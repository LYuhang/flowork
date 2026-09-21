"""write_file tool — write text content to a file (create or overwrite)."""
from __future__ import annotations

import hashlib

from vibecanvas_api.services.platform_mcp.tool_runtime import tool

from vibecanvas_api.agents.tools.decorator import tool_output, ToolError
from vibecanvas_api.agents.tools.render import register_render, Rendered
from vibecanvas_api.agents.tools import workspace_fs
from vibecanvas_api.services.file_format import content_type_for


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


@register_render("write_file")
def _render(raw: dict, ctx) -> Rendered:
    path = raw.get("path")
    line_count = raw.get("line_count")
    body = "ok"
    abstract = f"Wrote {path} ({line_count} line(s), {raw.get('bytes')} byte(s))"
    return Rendered(content=body, content_type=raw.get("content_type") or "text/plain",
                    abstract=abstract,
                    extras={"path": path, "content_hash": raw.get("content_hash"),
                            "bytes": raw.get("bytes"), "line_count": line_count})


@tool_output(content_type="application/json", tool="write_file", is_viewer=True)
async def _write_workspace(path: str, content: str) -> dict:
    """Write directly to a mounted workspace path. Durable writeback is owned by
    the host Runtime Orchestrator at the turn boundary. Returns
    ``{status:ok, path, bytes, content}``
    or raises a
    ToolError with a CLEAN message (never the raw backend string)."""
    try:
        res = await workspace_fs.write_file(path, content)
    except RuntimeError:                            # internal (no fileop pool) — don't leak
        raise ToolError("no_workspace",
                        "no workspace is available — file operations require "
                        "an active workspace sandbox")
    if not res.get("ok"):
        err = res.get("error") or ""
        if err == "path_outside_roots":
            raise ToolError("invalid_path", f"path {path!r} is outside the allowed roots")
        raise ToolError("write_failed", f"could not write {path!r}")
    return {"status": "ok", "path": path, "bytes": res.get("bytes"),
            "content": content, "content_type": content_type_for(path),
            "line_count": len(content.splitlines()),
            "content_hash": _content_hash(content)}


@tool(response_format="content_and_artifact")
async def write_file(path: str, content: str):
    """Write text content to a file, creating it or overwriting it.

    Use this when creating a new file or intentionally replacing the entire file
    with a complete new version. If the task only changes a small or localized
    part of an existing file, use edit_file instead so unchanged content is not
    unnecessarily regenerated or overwritten. For large structured files, prefer
    generating or mutating the file with a script and then write the final result,
    rather than hand-writing a long inline payload.

    Args:
        path: the destination file path.
        content: the complete text that should become the file content.

    Returns:
        content = "ok" on success. Metadata is stored in the tool artifact;
        call read_file when you need to inspect the current file content.
    """
    return await _write_workspace(path, content)
