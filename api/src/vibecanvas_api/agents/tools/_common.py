"""agents/tools/_common — small utilities shared across the agent's tools.

These are NOT tools — just cross-cutting helpers (the current workflow version
used for stale-artifact marking, and a tiny error-envelope formatter).
"""
from __future__ import annotations

import json

from vibecanvas_api.utils.versioning import version_str


def _current_version(ctx) -> str | None:
    get_meta = getattr(ctx.repo, "get_meta", None)
    if not callable(get_meta):
        return None
    wf_id = getattr(ctx, "wf_id", "")
    return version_str(get_meta(wf_id) or {})


def _err(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)


def _human_size(n: int) -> str:
    """Human-readable byte size (1024-based): 412B, 1.4KB, 2.3MB, …"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


# Re-export the canonical text/binary classifier (single source: services.file_format)
# under the name the fs tools already import.
from vibecanvas_api.services.file_format import is_text_content_type as _is_text_ct  # noqa: E402,F401
