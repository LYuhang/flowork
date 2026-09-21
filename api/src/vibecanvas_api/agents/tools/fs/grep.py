"""grep tool — search file contents with a regular expression (like `grep -rn`)."""
from __future__ import annotations

from vibecanvas_api.services.platform_mcp.tool_runtime import tool

from vibecanvas_api.agents.tools.decorator import tool_output, ToolError
from vibecanvas_api.agents.tools.render import register_render, Rendered
from vibecanvas_api.agents.tools import workspace_fs


@register_render("grep")
def _render(raw: dict, ctx) -> Rendered:
    lines = raw.get("matches", [])
    body = "\n".join(lines) if lines else "(no matches)"
    if raw.get("truncated"):
        body += "\n…[grep stopped at the safety limit — result incomplete; refine the pattern]"
    n = raw.get("match_count", len(lines))
    abstract = f"{n} matches" + (" (safety-limited)" if raw.get("truncated") else "")
    return Rendered(content=body, content_type="text/plain", abstract=abstract)


@tool_output(content_type="text/plain", tool="grep")
async def _grep_workspace(pattern: str, prefix: str, glob_filter: str,
                          context: int) -> dict:
    """Search directly in the current sandbox Runtime (recursive, binary-skipping;
    filename ``glob`` + ``context`` lines applied in the sandbox). Returns the raw grep
    payload or raises a ToolError with a CLEAN message (never the raw backend string)."""
    try:
        res = await workspace_fs.grep(
            pattern, prefix or "/", glob_filter, max(0, context)
        )
    except RuntimeError:                            # internal (no fileop pool) — don't leak
        raise ToolError("no_workspace",
                        "no workspace is available — file operations require "
                        "an active workspace sandbox")
    if not res.get("ok"):
        err = res.get("error") or ""
        if err == "invalid_regex":
            raise ToolError("invalid_regex", f"invalid regex {pattern!r}")
        if err == "path_outside_roots":
            raise ToolError("invalid_path", f"path {prefix!r} is outside the allowed roots")
        raise ToolError("grep_failed", f"could not search {prefix!r}")
    return {"matches": res.get("matches", []),
            "match_count": res.get("match_count", 0),
            "truncated": bool(res.get("truncated"))}


@tool(response_format="content_and_artifact")
async def grep(
    pattern: str, path: str = "/", glob: str = "", context: int = 0
) -> str:
    """Search file contents with a regular expression (like `grep -rn`).

    Args:
        pattern: a Python regular expression to search for.
        path: the directory to search under (default: root).
        glob: optional filename filter (e.g. "*.md").
        context: lines of surrounding context to show around each match (like
            `grep -C`; default 0 = matching lines only).

    Returns:
        content = matching lines as ``path:line:text`` (grep -rn style); with
        `context`, surrounding lines appear as ``path-line-text`` and ``--`` separates
        groups. "(no matches)" when nothing matches. The complete match set is returned;
        only a rare internal safety limit can cut it short (a note says so).
    """
    return await _grep_workspace(pattern, path, glob, context)
