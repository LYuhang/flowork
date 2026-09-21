"""read_file tool — the agent's CONTENT VIEWER.

read_file is the ONE tool that returns a file's FULL content (paged). Every other
tool guardrails large output down to a preview + a file ref and points the agent
here to see the whole thing (``is_viewer=True`` → no large-output truncation; paging
via ``offset``/``limit`` is what bounds a single read).

It reads INSIDE the workflow sandbox (``session.read_file``). ``content_type`` is
derived from the path (``services.file_format``) and carried on the artifact: the
frontend renders by it, and as the read ages the compaction middleware compresses by
it (code→signatures, JSON→field names, table→header+rows; §6.2). A binary file
returns a short descriptor instead of raw bytes.
"""
from __future__ import annotations

from vibecanvas_api.services.platform_mcp.tool_runtime import tool

from vibecanvas_api.agents.tools._common import _human_size
from vibecanvas_api.agents.tools.decorator import tool_output, ToolError
from vibecanvas_api.agents.tools.render import register_render, Rendered
from vibecanvas_api.agents.tools import workspace_fs
from vibecanvas_api.services.file_format import content_type_for


def _guess_ct(path: str) -> str:
    """A sandbox text read carries no stored content_type — derive the canonical one
    from the path (single source of truth: services.file_format)."""
    return content_type_for(path)


def _page_window(text: str, offset: int, limit: int) -> tuple[str, int, str | None]:
    """Line-based paging. offset = 1-based start line, limit = max lines; appends
    a truncation note when more lines remain below the window."""
    if offset <= 0 and limit <= 0:
        return text, 1, None
    lines = text.splitlines()
    total = len(lines)
    start = max(offset - 1, 0) if offset > 0 else 0
    end = start + limit if limit > 0 else total
    body = "\n".join(lines[start:end])
    note = None
    if end < total:
        note = (f"...[truncated; showing lines {start + 1}-{min(end, total)} of "
                f"{total} - read_file(offset={end + 1}) for more]")
    return body, start + 1, note


def _page(text: str, offset: int, limit: int) -> str:
    body, _start_line, note = _page_window(text, offset, limit)
    return f"{body}\n{note}" if note else body


def _text(body: str, content_type: str, path: str, *, start_line: int = 1,
          page_note: str | None = None) -> dict:
    return {"kind": "text", "body": body, "content_type": content_type or "text/plain",
            "path": path, "start_line": start_line, "page_note": page_note}


def _binary(content_type: str, path: str, size_bytes) -> dict:
    return {"kind": "binary", "content_type": content_type or "application/octet-stream",
            "path": path, "size_bytes": size_bytes}


@register_render("read_file")
def _render(raw: dict, ctx) -> Rendered:
    """read_file's presentation: content = the file body itself (viewer), with the
    file's OWN content_type (dynamic, per file) so frontend + compaction dispatch on
    it. Binary collapses to a one-line descriptor."""
    ct, path = raw["content_type"], raw["path"]
    if raw["kind"] == "binary":
        content = (f"[binary file: {ct}, {_human_size(raw.get('size_bytes') or 0)} — "
                   f"referenced by path, not loaded into context]")
        abstract = f"Read {path} — binary {ct}"
    else:
        content = raw["body"]
        if raw.get("page_note"):
            content += f"\n{raw['page_note']}"
        abstract = f"Read {path} ({ct})"
    return Rendered(content=content, content_type=ct, abstract=abstract, path=path)


@tool_output(content_type="text/plain", tool="read_file", is_viewer=True)
async def _read_workspace(path: str, offset: int, limit: int) -> dict:
    """Read directly from the current sandbox Runtime's Linux filesystem.

    Returns the raw
    read payload ({kind, body/content_type/path, …}; the render builds the envelope,
    ``is_viewer`` keeps the full body) or raises ToolError with a CLEAN message
    (never the raw backend string)."""
    try:
        res = await workspace_fs.read_file(path)
    except RuntimeError:                            # internal (no fileop pool) — never leak details
        raise ToolError("no_workspace",
                        "no workspace is available — file operations require "
                        "an active workspace sandbox")
    if not res.get("ok"):
        err = res.get("error") or ""
        if err == "not_found":
            raise ToolError("path_not_found", f"path {path!r} does not exist")
        if err == "path_outside_roots":
            raise ToolError("invalid_path", f"path {path!r} is outside the allowed roots")
        detail = f": {err}" if err else ""
        raise ToolError("read_failed", f"could not read {path!r}{detail}")
    if res.get("kind") == "binary":
        return _binary(res.get("content_type"), path, res.get("size"))
    body, start_line, page_note = _page_window(res.get("content", ""), offset, limit)
    return _text(body, _guess_ct(path), path, start_line=start_line, page_note=page_note)


@tool(response_format="content_and_artifact")
async def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a file's content — the full text (this is the content viewer).

    Other tools truncate large output to a preview and point you here; read_file
    returns the whole file. For a large file, page through it with `offset` (1-based
    start line) and `limit` (max lines). A binary file returns a short reference
    descriptor instead of its raw bytes.
    The path is read literally, not as a shell glob; use this for known file paths,
    especially paths containing spaces or shell metacharacters such as `[]`.

    Args:
        path: the file to read.
        offset: 1-based start line (0 = from the top).
        limit: max lines to return (0 = no line cap).

    Returns:
        content = the file text exactly as read for the requested page, or a
        short descriptor for a binary file.
    """
    return await _read_workspace(path, offset, limit)
