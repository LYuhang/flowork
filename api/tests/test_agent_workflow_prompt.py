"""Current Chat prompt boundaries."""
from vibecanvas_api.agents.prompts.compose import build_system_prompt
def _bsp(mode="chat"):
    modes = {"browser"} if mode == "browser" else set()
    return build_system_prompt(modes)


def test_chat_mode_prompt_has_lightweight_planning_no_orchestrator():
    p = _bsp("chat")
    assert isinstance(p, str) and len(p) > 0
    assert "You are the Flowork assistant" in p
    assert "## Conversation discipline" in p
    assert "## Platform message protocol" in p
    assert "ORCHESTRATOR" not in p
    # single source of truth: Base names no orchestration tools
    assert "run_phases" not in p and "set_plan" not in p


def test_agent_context_keeps_runtime_run_id_without_legacy_agent_plan():
    from vibecanvas_api.agent import AgentContext
    c = AgentContext()
    assert not hasattr(c, "agent_plan")
    assert hasattr(c, "run_id")


def test_agent_context_has_subagent_runtime_fields():
    from vibecanvas_api.agent import AgentContext
    ctx = AgentContext()
    assert ctx.agent_cfg is None
    assert ctx.stop_event is None


def test_chat_mode_planning_is_conservative_and_toolless():
    """Base prompt is conservative and names no orchestration tools."""
    p = _bsp("chat")
    low = p.lower()
    assert "conversation discipline" in low
    assert "set_plan" not in low and "run_phases" not in low
    assert "ORCHESTRATOR" not in p               # not the legacy orchestrator block
