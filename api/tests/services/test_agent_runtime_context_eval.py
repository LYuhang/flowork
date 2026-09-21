from __future__ import annotations
from types import SimpleNamespace

from vibecanvas_api.services.agent_runtime.context_manifest import (
    build_context_manifest,
    context_v2_rollout_mode,
)
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeInstruction,
    HostMcpServerAuthority,
)


def test_context_manifest_is_deterministic() -> None:
    instruction = RuntimeInstruction(
        instruction_id="command:build:v1",
        kind="command_context",
        scope="chat",
        name="workflow",
        version=1,
        content="Build safely.",
    )
    server = HostMcpServerAuthority(
        name="workflow",
        source="platform",
        connection={"transport": "host_gateway", "capability": "private"},
    )
    common = dict(
        rollout_mode="active",
        max_tokens=200_000,
        message={"content": "build it"},
        instructions=[instruction],
        mcp_servers=[server],
        skills=[],
        todo_items=[],
        artifact_refs={},
        workspace_scope_id="workspace-1",
        active_modes=["workflow"],
    )
    first = build_context_manifest(runtime_type="codex", **common)
    second = build_context_manifest(runtime_type="codex", **common)

    assert first.ordered_hash == second.ordered_hash
    assert [item.id for item in first.sections] == [
        item.id for item in second.sections
    ]
    assert first.adapter_mode == "observe_only"


def test_context_canary_assignment_is_stable() -> None:
    cfg = SimpleNamespace(
        rollout_mode="canary", canary_tenants=[], canary_percent=37
    )
    first = context_v2_rollout_mode(
        cfg, tenant_id="tenant-a", workspace_scope_id="workspace-a"
    )
    second = context_v2_rollout_mode(
        cfg, tenant_id="tenant-a", workspace_scope_id="workspace-a"
    )
    assert first == second
    assert first in {"shadow", "active"}
    cfg.canary_tenants = ["tenant-a"]
    assert context_v2_rollout_mode(
        cfg, tenant_id="tenant-a", workspace_scope_id="workspace-a"
    ) == "active"
