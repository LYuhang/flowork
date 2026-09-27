"""VFS 2b-2b — metadata+exec migration + fusion (unit tests)."""

from vibecanvas_api.storage.vfs_store import _EXT
from vibecanvas_api.agents.tools._envelope import _inline_or_omit


def test_ext_has_html():
    assert _EXT["text/html"] == "html"


def test_inline_or_omit_small_returns_value():
    small = [{"a": 1}]
    assert _inline_or_omit(small) == small


def test_inline_or_omit_large_returns_none():
    big = "x" * 20000  # > 16000 char cap
    assert _inline_or_omit(big) is None


def test_inline_or_omit_dict_under_cap():
    d = {"status": "ok", "node_outputs": {"n": 1}}
    assert _inline_or_omit(d) == d

def test_registry_has_fused_tools_not_retired():
    # Platform workflow execution is not a Runtime-private built-in,
    # while tabular work is handled through bash/Python instead of dedicated
    # tools.
    from vibecanvas_api.agents.tools import builtin_tool_names
    names = builtin_tool_names()
    assert "run_workflow" not in names
    assert names.isdisjoint({
        "inspect_data",
        "write_cells",
        "analyze_data_structure",
        "inspect_sheet",
        "run_node",
        "poll_task",
        "browse_template_market",
        "get_template_detail",
    })
