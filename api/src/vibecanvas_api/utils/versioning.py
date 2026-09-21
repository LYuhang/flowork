"""Workflow active-version formatting.

A leaf utility (no Agent SDK / DB imports) shared by the agent's read_file
tool and the VFS HTTP route, so the artifact `stale` flag can't drift between
the two code paths.
"""
from __future__ import annotations


def version_str(meta: dict) -> str | None:
    """Format a workflow-meta dict's active version as ``v{major}.sv{sub}``.

    Returns ``None`` when either ``active_major`` or ``active_sub`` is absent.
    """
    major, sub = meta.get("active_major"), meta.get("active_sub")
    if major is None or sub is None:
        return None
    return f"v{major}.sv{sub}"
