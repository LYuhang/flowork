"""compose.py — assemble the system prompt from modular blocks + active_modes.

The effective system prompt is Base (always) + one block per active mode, in a
DETERMINISTIC order so the prompt prefix is byte-stable when active_modes is
unchanged (prompt-prefix cache friendly).

  Base      : IDENTITY + MEMORY + CONVERSATION + SURFACE
  /workflow and /browser are persistent command contexts injected by
  CommandContextEdit, not system prompt fragments.
  (+ MCP catalog when MCPs are installed, always at a fixed tail position)
  (+ skill catalog when skills are available, always at a fixed tail position)

Single source of truth: blocks describe identity + protocol only. The MCP and
Skill catalogs list every installed capability and its authorized read-only
filesystem path.

The MCP catalog contains only the Chat-selected servers whose tools are already
connected for this Turn. The Skill catalog contains installed Skills and keeps
progressive disclosure through their mounted ``SKILL.md`` files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .identity import IDENTITY
from .memory import MEMORY
from .conversation import CONVERSATION
from .special import SPECIAL
from .surface import surface_prompt_for


def _conversation_time_block(
    conversation_clock: Mapping[str, Any],
) -> str:
    """Render one deterministic, conversation-scoped clock reference.

    The caller supplies a database-backed timestamp fixed on the first Turn.
    This function intentionally never calls ``now()`` so resume/reconnect does
    not perturb the provider prompt-prefix cache.
    """
    timezone_name = str(conversation_clock.get("timezone") or "UTC").strip()
    raw_started_at = conversation_clock.get("started_at")
    if isinstance(raw_started_at, datetime):
        started_at = raw_started_at
    else:
        started_at = datetime.fromisoformat(
            str(raw_started_at).replace("Z", "+00:00")
        )
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    try:
        user_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone_name = "UTC"
        user_zone = ZoneInfo("UTC")
    local_started_at = started_at.astimezone(user_zone)
    utc_started_at = started_at.astimezone(timezone.utc)
    return (
        "## Conversation time\n"
        "This is the user's fixed local-time reference captured when this "
        "conversation first started. It intentionally does not change during "
        "later turns or resumes.\n"
        f"- User timezone: `{timezone_name}`\n"
        f"- Conversation started locally: "
        f"`{local_started_at.isoformat(timespec='seconds')}`\n"
        f"- Same instant in UTC: `{utc_started_at.isoformat(timespec='seconds')}`"
    )


def _mcp_block(mcp_catalog: list[dict]) -> str:
    """Compact MCP server catalog injected into the system prompt.

    Each entry: {name, tool_count (int|None), loaded (bool)}.
    Connected servers are annotated so the agent knows it can call them immediately.
    """
    lines = ["## Available MCP servers"]
    lines.append(
        "These MCP servers were selected for this Project and are already "
        "connected. Use their namespaced tools directly; do not try to load "
        "additional MCP servers from inside the runtime."
    )
    for m in mcp_catalog:
        count = m.get("tool_count")
        tool_part = f" ({count} tools)" if count else ""
        status = " [connected]" if m.get("loaded") else ""
        desc = m.get("description") or ""
        desc_part = f" — {desc}" if desc else ""
        lines.append(f"- `{m['name']}`{tool_part}{status}{desc_part}")
    return "\n".join(lines)


def _skills_block(skill_catalog: list[dict]) -> str:
    """Compact skill catalog injected into the system prompt.

    Each entry: {name, description, root_path}.
    """
    lines = ["## Available skills"]
    lines.append(
        "All authorized Skills are listed below. When a task matches one, read "
        "its SKILL.md with the normal filesystem tools before following it. "
        "Skill mounts are read-only."
    )
    for m in skill_catalog:
        root_path = str(m.get("root_path") or "").rstrip("/")
        location = f" — `{root_path}/SKILL.md`" if root_path else ""
        lines.append(f"- `{m['name']}` — {m.get('description', '')}{location}")
    return "\n".join(lines)


def build_system_prompt(
    active_modes: set[str] | None = None,
    surface: str = "chat",
    mcp_catalog: list[dict] | None = None,
    skill_catalog: list[dict] | None = None,
    conversation_clock: Mapping[str, Any] | None = None,
) -> str:
    """Compose the system prompt for the given active_modes (deterministic order).

    ``mcp_catalog``: [{name, description, tool_count, loaded}] for all enabled MCP servers.
    ``skill_catalog``: [{name, description}] for all available skills (system + tenant).
    Both catalogs are optional — omit or pass empty list to skip the block.

    Commands such as ``build`` and ``browser`` do not alter the system prompt.
    They are injected near their latest activation message by CommandContextEdit.
    Any legacy/exclusive ``mode`` translation happens at the caller boundary
    (agent.py); this composer never sees it.
    """
    parts: list[str] = [IDENTITY, MEMORY, CONVERSATION, SPECIAL]  # Base, always
    if conversation_clock:
        parts.append(_conversation_time_block(conversation_clock))
    parts.append(surface_prompt_for(surface))
    if mcp_catalog:
        parts.append(_mcp_block(mcp_catalog))
    if skill_catalog:
        parts.append(_skills_block(skill_catalog))
    return "\n\n".join(p.strip("\n") for p in parts) + "\n"
