"""Command-mode prompt + tool composition."""
import pytest

from langchain_core.messages import HumanMessage

from vibecanvas_api.agents.commands import COMMAND_MODES, command_context_for
from vibecanvas_api.agents.prompts.compose import build_system_prompt
from vibecanvas_api.agents.token_accounting import message_tokens
from vibecanvas_api.agents.tools import build_tools


def _build_system_prompt(mode="chat", *, active_modes=None):
    """Test helper that folds browser routing into the active command set."""
    modes = set(active_modes or set())
    if mode == "browser":
        modes.add("browser")
    return build_system_prompt(modes, surface="chat")


_NODE_CATALOG_MARKER = "Node catalog"


# ── prompt split ──────────────────────────────────────────────────────────

def test_base_prompt_is_lean_no_node_definitions():
    p = _build_system_prompt("chat", active_modes=set())
    assert _NODE_CATALOG_MARKER not in p
    assert "Flowork assistant" in p
    assert "/workflow" in p
    assert "/browser" not in p
    # BUILD identity NOT present in Base
    assert "WORKFLOW mode" not in p


def test_base_prompt_default_active_modes_none_is_lean():
    # active_modes defaulting to None == Base lean (no node defs).
    p = _build_system_prompt("chat")
    assert _NODE_CATALOG_MARKER not in p


def test_active_workflow_does_not_change_system_prompt():
    p = _build_system_prompt("chat", active_modes={"workflow"})
    base = _build_system_prompt("chat", active_modes=set())
    assert p == base
    assert _NODE_CATALOG_MARKER not in p
    assert "prompt_template" not in p
    assert "Examples:" not in p


def test_workflow_command_context_has_schema_catalog_and_identity():
    p = command_context_for("workflow")
    assert _NODE_CATALOG_MARKER in p
    assert "WORKFLOW mode" in p
    assert "one top-level JSON object keyed by node id" in p
    assert "shared base" in p and "schema" in p
    assert "get_node_spec(node_type=...)" in p
    assert "`StartNode`" in p and "`PromptNode`" in p
    assert "prompt_template" in p
    assert "Compact example:" in p
    assert "#### Extended node catalog" in p
    assert "- `HTTPRequestNode`" in p
    assert "Workflow JSON shape" in p


def test_build_prompt_prefers_code_generated_workflow_json():
    p = command_context_for("workflow")
    assert "Treat workflow JSON as code-generated data" in p
    assert "json.dump(..., ensure_ascii=False, indent=2)" in p
    assert "JSON files must use double quotes" in p
    assert "python -m json.tool /data/workflow.json" in p


def test_build_prompt_separates_authoring_and_workflow_runtime_filesystems():
    p = command_context_for("workflow")
    assert "Authoring filesystem versus Workflow runtime" in p
    assert "Workflow nodes can access only `/run/...` and `/mount/...`" in p
    assert "Never invent a runtime file" in p
    assert "Agent-side `/data/...`" in p


def test_base_prompt_documents_workspace_visibility():
    p = _build_system_prompt("chat")
    assert "Workspace folders and visibility" in p
    assert "Chat/Agent workspace only" in p
    assert "visible to both Chat/Agent tools and Workflow nodes" in p
    assert "Node runtime file paths must use `/run/...` or `/mount/...`" in p


def test_build_prompt_requires_current_global_model_discovery():
    p = command_context_for("workflow")
    assert 'call `get_config(scope="global")` in the current workflow turn' in p
    assert "must be one enabled key" in p
    assert "Chat Agent's own model" in p


def test_build_prompt_warns_against_shared_end_node_for_branches():
    p = command_context_for("workflow")
    assert "Do not merge multiple branches by pointing them to the same EndNode" in p
    assert "Each terminal branch owns its own EndNode" in p
    assert "proper join node such as ParallelEndNode or LoopEndNode" in p


def test_build_prompt_still_has_lean_base_identity():
    # /workflow is additive on top of the lean base, not a replacement.
    p = _build_system_prompt("chat", active_modes={"workflow"})
    assert "Flowork assistant" in p


# ── tool split ────────────────────────────────────────────────────────────

def _names(tools):
    return {t.name for t in tools}


def test_base_tools_lack_construction_and_run_tools():
    names = _names(build_tools(set()))
    assert "vibe_workflow" not in names
    assert "check_workflow" not in names
    assert "save_workflow" not in names
    # RUN tools are BUILD-only now (testing goes with constructing).
    assert "run_workflow" not in names
    assert "node_execute" not in names
    # Data/background capabilities are supplied by always-on platform MCPs.
    assert {"read_file", "todo", "bash"} <= names
    assert {"read_cells", "query_data", "count_column_values"}.isdisjoint(names)
    assert "update_state" not in names


def test_build_tools_are_supplied_only_by_platform_mcp():
    base = _names(build_tools(set()))
    active = _names(build_tools({"workflow"}))
    platform_names = set(COMMAND_MODES["workflow"].tools)

    assert active == base
    assert platform_names.isdisjoint(active)


def test_build_mode_appends_without_reordering_base_prefix():
    base = [t.name for t in build_tools(set())]
    build = [t.name for t in build_tools({"workflow"})]
    assert build[:len(base)] == base


def test_command_mode_names_match_platform_mcp_groups():
    from vibecanvas_api.services.platform_mcp.build_tools import BUILD_TOOLS
    from vibecanvas_api.services.platform_mcp.build_tools.workflow_context import (
        create_workflow,
        set_workflow,
    )
    from vibecanvas_api.services.platform_mcp.run_tools import RUN_TOOLS
    from vibecanvas_api.services.platform_mcp.workflow_tools import (
        WORKFLOW_MCP_TOOLS,
    )

    cfg = COMMAND_MODES["workflow"]
    expected = {
        *(tool.name for tool in WORKFLOW_MCP_TOOLS),
        set_workflow.name,
        create_workflow.name,
        *(tool.name for tool in BUILD_TOOLS),
        *(tool.name for tool in RUN_TOOLS),
    }
    assert set(cfg.tools) == expected


async def _capture_agent_tools(active_modes):
    """Run ``_get_or_create_agent`` with create_agent/_build_chat_model patched
    and return the set of tool NAMES handed to create_agent."""
    from unittest.mock import patch, MagicMock
    import vibecanvas_api.agent as agent_mod

    captured: dict = {}

    def fake_create_agent(*, model, tools, **kwargs):
        captured["tools"] = tools
        return MagicMock(name="agent")

    with patch("vibecanvas_api.agent._build_chat_model", return_value="stub-model"), \
         patch("vibecanvas_api.agent.create_agent", side_effect=fake_create_agent):
        await agent_mod._get_or_create_agent(
            agent_cfg={"model": "Echo"},
            checkpointer=None,
            tenant_id=None,
            active_modes=active_modes,
        )
    return {t.name for t in captured["tools"]}


@pytest.mark.asyncio
async def test_agent_tool_list_gates_vibe_workflow_on_build():
    base_names = await _capture_agent_tools(set())
    assert "vibe_workflow" not in base_names
    assert "check_workflow" not in base_names
    assert "save_workflow" not in base_names
    # RUN tools are BUILD-only — NOT in plain Base.
    assert "run_workflow" not in base_names
    assert "node_execute" not in base_names
    assert {"read_file", "todo", "bash"} <= base_names
    assert {"read_cells", "query_data", "count_column_values"}.isdisjoint(base_names)
    assert "update_state" not in base_names

    build_names = await _capture_agent_tools({"workflow"})
    assert build_names == base_names
    assert set(COMMAND_MODES["workflow"].tools).isdisjoint(build_names)


# ── CommandContextEdit (persistent command context) ───────────────────────

def _human(text, *, command=None):
    kwargs = {"command_activation": {"name": command, "trigger": f"/{command}"}} if command else {}
    return HumanMessage(content=text, additional_kwargs=kwargs)


def test_command_context_prepends_latest_workflow_activation_message():
    from vibecanvas_api.agents.middleware.command_context_edit import CommandContextEdit
    msgs = [
        _human("/workflow first task", command="workflow"),
        _human("normal follow-up"),
        _human("/workflow second task", command="workflow"),
    ]
    CommandContextEdit(
        {"workflow": command_context_for("workflow")},
        {"workflow"},
    ).apply(msgs, count_tokens=lambda xs: 123)
    injected = [
        m for m in msgs
        if '<command-context command="workflow">' in getattr(m, "content", "")
    ]
    assert len(injected) == 1
    assert len(msgs) == 3
    assert msgs.index(injected[0]) == 2
    content = injected[0].content
    assert content.startswith("<system-reminder>")
    assert "<system-reminder>" in content and "</system-reminder>" in content
    assert "<user-message>\n/workflow second task\n</user-message>" in content
    assert "WORKFLOW mode" in content
    assert "latest /workflow activation" in content
    assert "get_node_spec(node_type=\"StartNode\")" not in content
    assert injected[0].additional_kwargs["tokens"]["raw"] == 123
    assert message_tokens(injected[0])["raw"] == 123
    assert '<command-context-superseded command="workflow">' in msgs[0].content
    assert "<user-message>\n/workflow first task\n</user-message>" in msgs[0].content
    assert msgs[0].additional_kwargs["command_context"]["workflow"] == {
        "injected": False,
        "projection": "superseded",
    }
    assert msgs[2].additional_kwargs["command_context"]["workflow"] == {
        "injected": True,
        "projection": "active",
    }


def test_command_context_counts_active_and_superseded_repeated_commands():
    from vibecanvas_api.agents.middleware.command_context_edit import CommandContextEdit
    calls = 0

    def count_tokens(_msgs):
        nonlocal calls
        calls += 1
        return 321

    msgs = [
        _human("/workflow first task", command="workflow"),
        _human("normal follow-up"),
        _human("/workflow second task", command="workflow"),
    ]
    CommandContextEdit(
        {"workflow": command_context_for("workflow")},
        {"workflow"},
    ).apply(msgs, count_tokens=count_tokens)
    injected = [
        m for m in msgs
        if '<command-context command="workflow">' in getattr(m, "content", "")
    ]
    assert len(injected) == 1
    assert calls == 2
    assert len(msgs) == 3
    assert msgs.index(injected[0]) == 2
    assert message_tokens(injected[0])["raw"] == 321
    assert message_tokens(msgs[0])["raw"] == 321


def test_command_context_supersession_is_per_command_and_idempotent():
    from vibecanvas_api.agents.middleware.command_context_edit import CommandContextEdit

    msgs = [
        _human("/workflow first task", command="workflow"),
        _human("/browser inspect first page", command="browser"),
        _human("/workflow second task", command="workflow"),
        _human("/browser inspect second page", command="browser"),
    ]
    edit = CommandContextEdit(
        {
            "workflow": "BACKEND WORKFLOW CONTEXT",
            "browser": "BACKEND BROWSER CONTEXT",
        },
        {"workflow", "browser"},
    )
    edit.apply(msgs, count_tokens=lambda xs: len(xs[0].content))
    first_projection = [message.content for message in msgs]
    edit.apply(msgs, count_tokens=lambda xs: len(xs[0].content))

    assert [message.content for message in msgs] == first_projection
    assert '<command-context-superseded command="workflow">' in msgs[0].content
    assert '<command-context-superseded command="browser">' in msgs[1].content
    assert "BACKEND WORKFLOW CONTEXT" in msgs[2].content
    assert "BACKEND BROWSER CONTEXT" in msgs[3].content
    assert "BACKEND BROWSER CONTEXT" not in msgs[2].content
    assert "BACKEND WORKFLOW CONTEXT" not in msgs[3].content


def test_command_context_projection_does_not_change_checkpoint_source_messages():
    from copy import deepcopy

    from vibecanvas_api.agents.middleware.command_context_edit import CommandContextEdit

    checkpoint_messages = [
        _human("first task", command="workflow"),
        _human("second task", command="workflow"),
    ]
    model_view = deepcopy(checkpoint_messages)
    CommandContextEdit(
        {"workflow": "BACKEND WORKFLOW CONTEXT"},
        {"workflow"},
    ).apply(model_view, count_tokens=lambda xs: 1)

    assert [message.content for message in checkpoint_messages] == [
        "first task",
        "second task",
    ]
    assert all(
        "command_context" not in message.additional_kwargs
        for message in checkpoint_messages
    )
    assert "<command-context-superseded" in model_view[0].content
    assert "BACKEND WORKFLOW CONTEXT" in model_view[1].content


def test_command_context_noop_when_empty():
    from vibecanvas_api.agents.middleware.command_context_edit import CommandContextEdit
    msgs = [_human("hello")]
    CommandContextEdit({}, set()).apply(msgs, count_tokens=lambda xs: 0)
    assert len(msgs) == 1
    assert msgs[0].content == "hello"


def test_command_context_uses_backend_content_for_implicit_activation():
    from vibecanvas_api.agents.middleware.command_context_edit import CommandContextEdit

    msgs = [_human("inspect this page")]
    CommandContextEdit(
        {"browser": "BACKEND-RESOLVED BROWSER CONTEXT"},
        {"browser"},
    ).apply(msgs, count_tokens=lambda xs: 17)

    assert "BACKEND-RESOLVED BROWSER CONTEXT" in msgs[0].content
    assert "<user-message>\ninspect this page\n</user-message>" in msgs[0].content


def test_command_context_fail_soft_on_bad_config():
    from vibecanvas_api.agents.middleware.command_context_edit import CommandContextEdit
    msgs = []
    CommandContextEdit({}, set()).apply(msgs, count_tokens=lambda xs: 0)
    assert msgs == []


def test_context_edits_inserts_command_context_before_compaction():
    from vibecanvas_api.agent import _build_context_edits
    edits = _build_context_edits(
        None,
        command_contexts={"workflow": command_context_for("workflow")},
        activated_this_turn={"workflow"},
    )
    types = [type(e).__name__ for e in edits]
    assert "CommandContextEdit" in types
    assert types.index("CommandContextEdit") < types.index("LifecyclePolicyEdit")


def test_context_edits_no_command_context_when_empty():
    from vibecanvas_api.agent import _build_context_edits
    edits = _build_context_edits(None, command_contexts={})
    types = [type(e).__name__ for e in edits]
    assert "CommandContextEdit" not in types


def test_browser_mode_does_not_change_system_prompt():
    p = _build_system_prompt("browser")
    assert p == _build_system_prompt("chat", active_modes=set())
    assert "## Browser mode" not in p
    assert "browser_snapshot" not in p


def test_browser_tools_are_supplied_only_by_official_playwright_mcp():
    from vibecanvas_api.agents.tools import build_tools
    from vibecanvas_api.browser.playwright_contract import PLAYWRIGHT_AGENT_TOOL_SET
    bnames = set(PLAYWRIGHT_AGENT_TOOL_SET)
    def names(ts):
        return {t.name for t in ts}
    assert not (bnames & names(build_tools(set())))
    assert names(build_tools({"browser"})) == names(build_tools(set()))
    assert bnames == set(COMMAND_MODES["browser"].tools)
