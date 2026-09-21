"""edit_file tool — exact string replacement in a text file."""
from __future__ import annotations

import hashlib

from vibecanvas_api.services.platform_mcp.tool_runtime import tool

from vibecanvas_api.agents.tools.decorator import tool_output, ToolError
from vibecanvas_api.agents.tools.render import register_render, Rendered
from vibecanvas_api.agents.tools import workspace_fs
from vibecanvas_api.services.file_format import content_type_for


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


@register_render("edit_file")
def _render(raw: dict, ctx) -> Rendered:
    """edit_file's presentation: only the diff content.

    Write metadata stays in the artifact/extras so it remains debuggable without
    being mixed into model-facing file text.
    """
    path = raw.get("path")
    msg = f"Edited {path} ({raw.get('replacements')} replacement(s))"
    diff = raw.get("diff")
    content = diff or "ok"
    return Rendered(content=content, content_type="text/x-diff",
                    abstract=msg,
                    extras={"path": path, "content_hash": raw.get("content_hash"),
                            "line_count": raw.get("line_count")})


@tool_output(content_type="application/json", tool="edit_file", is_viewer=True)
async def _edit_workspace(path: str, old_string: str, new_string: str,
                          replace_all: bool) -> dict:
    """Edit directly in the current sandbox Runtime — read +
    exact-replace + write all happen against the sandbox. Returns
    ``{status:ok, path, replacements, diff, content}`` or raises a ToolError with a
    CLEAN message (never the raw backend string)."""
    if old_string == new_string:
        raise ToolError("no_change", "old_string and new_string are identical — nothing to change")
    try:
        res = await workspace_fs.edit_file(path, old_string, new_string, replace_all)
    except RuntimeError:                            # internal (no fileop pool) — don't leak
        raise ToolError("no_workspace",
                        "no workspace is available — file operations require "
                        "an active workspace sandbox")
    if not res.get("ok"):
        err = res.get("error") or ""
        if err == "not_found":
            raise ToolError("not_found",
                            f"old_string not found in {path!r} (or the file does not exist) — it "
                            f"must match exactly (check whitespace/indentation); read the file first")
        if err == "not_unique":
            raise ToolError("not_unique",
                            f"old_string is not unique in {path!r} — add surrounding context to "
                            f"disambiguate, or pass replace_all=True")
        if err == "not_text":
            raise ToolError("not_text", f"{path!r} is not a text file")
        if err == "path_outside_roots":
            raise ToolError("invalid_path", f"path {path!r} is outside the allowed roots")
        raise ToolError("edit_failed", f"could not edit {path!r}")
    content = res.get("content")
    payload = {"status": "ok", "path": path, "replacements": res.get("replacements"),
               "diff": res.get("diff"), "content_type": content_type_for(path)}
    if isinstance(content, str):
        payload.update({"content": content, "line_count": len(content.splitlines()),
                        "content_hash": _content_hash(content)})
    return payload


@tool(response_format="content_and_artifact")
async def edit_file(path: str, old_string: str, new_string: str,
                    replace_all: bool = False) -> str:
    """Exact string replacement in a file. `old_string` must match exactly and be
    unique. Read the full current file content first, then copy the exact
    unchanged surrounding text into old_string so whitespace, indentation, quotes,
    and line breaks match the file. Use this for localized edits to existing
    files; use write_file only when the whole file should be replaced.

    Args:
        path: the file to edit.
        old_string: the exact current file text to replace (must be unique unless
            replace_all). Do not invent this from memory; read the file first.
        new_string: the replacement text.
        replace_all: replace every occurrence instead of erroring on non-unique.

    Returns:
        content = the changed-region diff only, or "ok" when the backend does
        not return a diff. Metadata such as path, hash, line count, and
        replacement count is stored in the tool artifact instead of being mixed
        into the model-facing text.
    """
    return await _edit_workspace(path, old_string, new_string, replace_all)
