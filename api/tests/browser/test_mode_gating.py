from vibecanvas_api.agents.tools import builtin_tool_names
from vibecanvas_api.browser.playwright_contract import PLAYWRIGHT_AGENT_TOOL_SET

_BROWSER_NAMES = set(PLAYWRIGHT_AGENT_TOOL_SET)


def _names(tools):
    return {getattr(t, "name", getattr(t, "__name__", "")) for t in tools}


def test_runtime_private_tools_never_register_platform_browser_tools():
    assert not (_BROWSER_NAMES & builtin_tool_names())


def test_mode_literal_accepts_browser():
    from vibecanvas_api.schemas.chat import MessagePostBody
    body = MessagePostBody(content="hi", mode="browser")
    assert body.mode == "browser"
