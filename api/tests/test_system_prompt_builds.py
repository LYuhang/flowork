"""Regression: the build prompt and on-demand node specs assemble without raising."""
from vibecanvas_api.agents.prompts.compose import build_system_prompt
from vibecanvas_api.agents.prompts.node_definitions import (
    available_node_types,
    build_node_spec,
    core_build_node_types,
    format_node_catalog_for_prompt,
    format_node_spec,
)


def test_on_demand_node_spec_does_not_raise():
    spec = build_node_spec("PromptNode", {})
    out = format_node_spec(spec)
    assert "Node spec: PromptNode" in out
    assert "CONFIG_SCHEMA" in out
    assert "prompt_template" in out
    assert "flowork-cli config get --scope model_api" in out
    assert "Never use the chat Agent" in out


def test_subagent_node_is_registered_for_on_demand_spec():
    assert "SubAgentNode" in available_node_types()
    out = format_node_spec(build_node_spec("SubAgentNode", {}))
    assert "Node spec: SubAgentNode" in out
    assert "task_template" in out
    assert "flowork-cli config get --scope model_api" in out


def test_build_system_prompt_does_not_raise():
    out = build_system_prompt()
    assert isinstance(out, str) and len(out) > 100
    # Active commands no longer alter the system prompt; command context is
    # injected near the latest command activation message.
    built = build_system_prompt({"workflow"})
    assert built == out
    assert "Node catalog" not in built
    assert "prompt_template" not in built


def test_conversation_clock_is_timezone_aware_and_byte_stable():
    clock = {
        "timezone": "Asia/Shanghai",
        "started_at": "2026-08-02T16:30:45+00:00",
    }
    first = build_system_prompt(conversation_clock=clock)
    resumed = build_system_prompt(conversation_clock=dict(clock))

    assert first == resumed
    assert "Conversation time" in first
    assert "Asia/Shanghai" in first
    assert "2026-08-03T00:30:45+08:00" in first
    assert "does not change during later turns or resumes" in first


def test_build_prompt_node_catalog_embeds_core_specs_and_extended_catalog():
    out = format_node_catalog_for_prompt({})
    assert "#### Core node specs" in out
    assert "#### `StartNode`" in out
    assert "#### `PromptNode`" in out
    assert "#### `SubAgentNode`" in out
    assert "#### `HumanApprovalNode`" in out
    assert "CONFIG_SCHEMA:" in out
    assert "Compact example:" in out
    assert "#### Extended node catalog" in out
    assert "- `HTTPRequestNode`" in out
    assert "run `flowork-cli workflow get-spec --type <NodeType>` for exact schema" in out


def test_core_build_node_types_are_expected_flow_nodes():
    assert core_build_node_types() == (
        "StartNode",
        "EndNode",
        "CodeNode",
        "PromptNode",
        "SubAgentNode",
        "ConditionNode",
        "HumanApprovalNode",
        "ParallelStartNode",
        "ParallelEndNode",
        "LoopBeginNode",
        "LoopEndNode",
    )
