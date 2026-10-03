"""Canonical Project workspace identities; routing keys, never authorization."""

from __future__ import annotations

import base64
import re


_PROJECT_PREFIX = "__projectws_v1_"


def chat_working_directory(chat_id: str) -> str:
    """A persistent thread directory within its Project, not an isolation root."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", chat_id):
        raise ValueError("invalid chat_id for workspace directory")
    return f"/chats/{chat_id}"


def project_workspace_scope_id(project_id: str, *, workflow_id: str | None = None) -> str:
    """Return the deterministic VFS/sandbox scope for one Project.

    A separate namespace prevents Project IDs from being resolved as Chat IDs.
    Ownership is always checked against the durable resource row.
    """
    # workflow_id must come from an authorized persisted Project binding.
    # It joins canvas Chat and run to the same VFS and sandbox identity.
    if workflow_id is not None:
        if not workflow_id or workflow_id.startswith("__"):
            raise ValueError("invalid workflow workspace identity")
        return workflow_id
    if not project_id:
        raise ValueError("project_id must not be empty")
    encoded = base64.urlsafe_b64encode(project_id.encode("utf-8")).rstrip(b"=").decode("ascii")
    return _PROJECT_PREFIX + encoded


def project_id_from_workspace_scope(scope_id: str) -> str | None:
    return _decode_scope(scope_id, _PROJECT_PREFIX)


def is_agent_workspace_scope(scope_id: str) -> bool:
    return project_id_from_workspace_scope(scope_id) is not None


def _decode_scope(scope_id: str, prefix: str) -> str | None:
    if not scope_id or not scope_id.startswith(prefix):
        return None
    encoded = scope_id[len(prefix):]
    if not encoded:
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not value or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != encoded:
        return None
    return value


__all__ = [
    "chat_working_directory",
    "project_workspace_scope_id",
    "project_id_from_workspace_scope",
    "is_agent_workspace_scope",
]
