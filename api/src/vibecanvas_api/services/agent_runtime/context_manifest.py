"""Runtime-neutral context inventory and deterministic rollout helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeContextManifest,
    RuntimeContextSection,
    RuntimeInstruction,
    HostMcpServerAuthority,
    RuntimeSkill,
)


def _token_estimate(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    )
    return max(1, (len(text) + 3) // 4) if text else 0


def context_v2_rollout_mode(
    cfg: Any,
    *,
    tenant_id: str,
    workspace_scope_id: str,
) -> str:
    """Resolve off/shadow/active with a deterministic tenant/workspace canary."""
    mode = str(getattr(cfg, "rollout_mode", "shadow") or "shadow").lower()
    if mode in {"off", "shadow", "full"}:
        return "active" if mode == "full" else mode
    if mode != "canary":
        return "shadow"
    tenants = {str(value) for value in getattr(cfg, "canary_tenants", [])}
    if tenant_id in tenants:
        return "active"
    percent = max(0, min(100, int(getattr(cfg, "canary_percent", 0))))
    bucket = int(hashlib.sha256(
        f"{tenant_id}:{workspace_scope_id}".encode("utf-8")
    ).hexdigest()[:8], 16) % 100
    return "active" if bucket < percent else "shadow"


def build_context_manifest(
    *,
    runtime_type: str,
    rollout_mode: str,
    max_tokens: int,
    message: dict[str, Any],
    instructions: Iterable[RuntimeInstruction],
    mcp_servers: Iterable[HostMcpServerAuthority],
    skills: Iterable[RuntimeSkill],
    todo_items: list[dict[str, Any]],
    artifact_refs: dict[str, dict[str, Any]],
    workspace_scope_id: str,
    active_modes: Iterable[str],
) -> RuntimeContextManifest:
    """Build a stable product-fact manifest before either adapter runs.

    Adapters may add model-native history decisions, but the shared resources
    and their ordering remain identical across Runtime adapters.
    """
    sections: list[RuntimeContextSection] = []

    def add(
        section_id: str,
        kind: str,
        source: str,
        priority: int,
        value: Any,
        retention: str,
    ) -> None:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        sections.append(RuntimeContextSection(
            id=section_id,
            kind=kind,
            source=source,
            priority=priority,
            token_estimate=_token_estimate(value),
            retention=retention,
            content_hash="sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ))

    add("policy:runtime", "policy", "platform.runtime", 100, {
        "approval": True,
    }, "pinned")
    add("workspace:binding", "workspace", "platform.chat", 95, {
        "workspace_scope_id": workspace_scope_id,
        "active_modes": sorted(set(active_modes)),
    }, "pinned")
    for instruction in sorted(instructions, key=lambda item: item.instruction_id):
        add(
            f"instruction:{instruction.instruction_id}",
            "system",
            instruction.instruction_id,
            90,
            instruction.content,
            "pinned",
        )
    add("history:current_intent", "history", "turn.user_message", 100, message, "pinned")
    if todo_items:
        add("memory:todo", "memory", "chat.todo", 92, todo_items, "summarize")
    if artifact_refs:
        add("artifacts:durable_refs", "artifact_ref", "chat.artifacts", 88,
            artifact_refs, "reloadable")
    for skill in sorted(skills, key=lambda item: (item.name.casefold(), item.skill_id)):
        add(f"skill:{skill.skill_id}", "system", skill.root_path, 70, {
            "name": skill.name,
            "description": skill.description,
            "revision_hash": skill.revision_hash,
        }, "reloadable")
    for server in sorted(mcp_servers, key=lambda item: (item.source, item.name)):
        add(f"tools:{server.source}:{server.name}", "tool_schema",
            f"mcp:{server.name}", 75 if server.required else 60, {
                "name": server.name,
                "source": server.source,
                "description": server.description,
                "config_revision": server.config_revision,
                "required": server.required,
            }, "reloadable")

    sections.sort(key=lambda item: (-item.priority, item.kind, item.id))
    digest = hashlib.sha256(json.dumps(
        [item.model_dump(mode="json") for item in sections],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return RuntimeContextManifest(
        mode=rollout_mode,
        adapter_mode=(
            "observe_only" if runtime_type == "codex" else
            ("active" if rollout_mode == "active" else "observe_only")
        ),
        budget={
            "max_tokens": max(1, max_tokens),
            "target_tokens": max(1, int(max_tokens * 0.5)),
        },
        sections=sections,
        ordered_hash=f"sha256:{digest}",
    )
