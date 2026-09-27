from vibecanvas_api.agents.tools import builtin_tool_names


def test_runtime_private_tools_never_register_platform_browser_tools():
    assert not any(name.startswith("browser_") for name in builtin_tool_names())


def test_mode_literal_accepts_browser():
    from vibecanvas_api.schemas.chat import MessagePostBody
    body = MessagePostBody(content="hi", mode="browser")
    assert body.mode == "browser"
