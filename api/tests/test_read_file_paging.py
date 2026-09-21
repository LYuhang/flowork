"""read_file line-based paging plus the Runtime-neutral private tool surface."""
from vibecanvas_api.agents.tools.fs.read_file import _page


def test_page_full_when_within_limit():
    text = "a\nb\nc"
    assert _page(text, 0, 2000) == "a\nb\nc"


def test_page_offset_and_limit_window():
    text = "\n".join(str(i) for i in range(1, 11))  # lines "1".."10"
    out = _page(text, 3, 4)        # 1-based start line 3, 4 lines → 3,4,5,6
    assert out.startswith("3\n4\n5\n6")
    assert "truncated" in out.lower()
    assert "lines 3-6 of 10" in out


def test_page_last_window_no_truncation_note():
    text = "\n".join(str(i) for i in range(1, 6))  # 5 lines
    out = _page(text, 3, 10)       # from line 3 to end → no more remaining
    assert out == "3\n4\n5"
    assert "truncated" not in out.lower()


def test_page_offset_zero_limit_zero_is_identity():
    text = "x\ny\nz"
    assert _page(text, 0, 0) == text


def test_projection_reads_are_platform_mcp_only():
    from vibecanvas_api.agents.tools import builtin_tool_names
    base = builtin_tool_names()
    # Cross-Runtime build capabilities are supplied exclusively by Platform MCP,
    # never disguised as Runtime-private tools.
    assert "get_workflow" not in base and "get_template" not in base
    # the clean industry FS surface lives in Base (ls/glob retired → bash/grep)
    assert {"read_file", "write_file", "edit_file", "grep", "bash"} <= base
    assert "update_state" not in base
