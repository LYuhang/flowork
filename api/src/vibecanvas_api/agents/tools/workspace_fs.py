"""Workspace mount roots shared by native Workflow SubAgent tools.

The sandbox owns isolation and mount permissions. No Chat-specific environment
flag or second SandboxSession is needed. Image reads also check containment;
the private /runtime mount is never a workspace root.
"""
from __future__ import annotations

import os

_WORKSPACE_ROOTS = ("/data", "/memory", "/logs", "/mount", "/run", "/runs", "/work")
_READ_ONLY_ROOTS = ("/skills",)


def roots(*, include_read_only: bool = False) -> list[str]:
    candidates = (
        (*_WORKSPACE_ROOTS, *_READ_ONLY_ROOTS)
        if include_read_only
        else _WORKSPACE_ROOTS
    )
    available = [path for path in candidates if os.path.isdir(path)]
    if not available:
        raise RuntimeError("workspace has no mounted roots")
    return available
