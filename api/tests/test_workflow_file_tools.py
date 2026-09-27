from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("graph,expected", [
    ({"__meta__": None}, "__meta__ must be an object."),
    ({"node": []}, "Each workflow node must be an object."),
    ({"node": {"node_type": []}}, "node_type must be a string."),
    ({"node": {"node_type": "PromptNode", "node_config": []}}, "node_config must be an object."),
    ({"node": {"node_type": "StartNode", "children": [{}]}}, "children must be an array of node ID strings."),
])
def test_static_validation_returns_english_errors_for_malformed_shapes(graph, expected):
    from vibecanvas_api.services.agent_resources.workflow_graph import (
        collect_workflow_warnings, validate_workflow, validate_workflow_model_names,
    )

    assert expected in [item["message"] for item in validate_workflow(graph)]
    assert collect_workflow_warnings(graph) == []
    assert validate_workflow_model_names(graph, []) == []


def test_static_validation_reuses_engine_and_registered_node_checks(monkeypatch):
    from vibecanvas_api.services.agent_resources import workflow_graph as workflow_file

    graph = {"node": {"node_type": "StaticOnlyTestNode", "node_name": "用户名称"}}
    calls = []

    def check_graph(value):
        assert value is graph
        calls.append("graph")
        return {"status": "error", "error_message": "Invalid graph reference."}

    def check_node(value):
        assert value is graph["node"]
        calls.append("node")
        return {"status": "error", "error_message": "Missing node configuration."}

    monkeypatch.setattr(workflow_file.Workflow, "check", check_graph)
    monkeypatch.setitem(workflow_file.node_registry._module_dict, "StaticOnlyTestNode", SimpleNamespace(check=check_node))
    assert workflow_file.validate_workflow(graph) == [
        {"node_id": "global", "message": "Invalid graph reference."},
        {"node_id": "node", "message": "Missing node configuration."},
    ]
    assert calls == ["graph", "node"]
    assert graph["node"]["node_name"] == "用户名称"


def test_auto_tidy_workflow_spreads_graph_left_to_right():
    from vibecanvas_api.services.agent_resources.workflow_graph import _auto_tidy_workflow

    wf = {
        "node_1": {"node_id": "node_1", "node_type": "StartNode", "children": ["node_2", "node_3"], "__attributes__": {"x": 0, "y": 0}},
        "node_2": {"node_id": "node_2", "node_type": "CodeNode", "children": ["node_4"], "__attributes__": {"x": 0, "y": 0}},
        "node_3": {"node_id": "node_3", "node_type": "CodeNode", "children": ["node_4"], "__attributes__": {"x": 0, "y": 0}},
        "node_4": {"node_id": "node_4", "node_type": "EndNode", "children": [], "__attributes__": {"x": 0, "y": 0}},
    }

    _auto_tidy_workflow(wf)

    assert wf["node_1"]["__attributes__"]["x"] == 0
    assert wf["node_2"]["__attributes__"]["x"] == wf["node_3"]["__attributes__"]["x"]
    assert wf["node_2"]["__attributes__"]["x"] > wf["node_1"]["__attributes__"]["x"]
    assert wf["node_2"]["__attributes__"]["y"] != wf["node_3"]["__attributes__"]["y"]
    assert wf["node_4"]["__attributes__"]["x"] > wf["node_2"]["__attributes__"]["x"]


def test_auto_tidy_workflow_preserves_parallel_branch_lanes():
    """A short branch's long join edge must not cross a longer sibling node.

    This is the shape produced by a ParallelStart whose first branch contains
    a loop/code chain while its second branch goes directly to ParallelEnd.
    The old per-rank centring put node_9 and node_12 on y=0, so the visual edge
    node_12 -> node_18 appeared to terminate at node_9.
    """
    from vibecanvas_api.services.agent_resources.workflow_graph import _auto_tidy_workflow

    def node(node_id, children):
        return {
            "node_id": node_id,
            "node_type": "CodeNode",
            "children": children,
            "__attributes__": {"x": 0, "y": 0},
        }

    wf = {
        "node_7": node("node_7", ["node_8", "node_12", "node_15"]),
        "node_8": node("node_8", ["node_9"]),
        "node_9": node("node_9", ["node_10"]),
        "node_10": node("node_10", ["node_11"]),
        "node_11": node("node_11", ["node_18"]),
        "node_12": node("node_12", ["node_18"]),
        "node_15": node("node_15", ["node_18"]),
        "node_18": node("node_18", []),
    }

    _auto_tidy_workflow(wf)

    y = lambda node_id: wf[node_id]["__attributes__"]["y"]
    x = lambda node_id: wf[node_id]["__attributes__"]["x"]

    # The long branch stays in node_8's lane instead of being re-centred onto
    # the short node_12 -> node_18 edge at each otherwise-singleton rank.
    assert y("node_9") == y("node_8")
    assert y("node_10") == y("node_8")
    assert y("node_11") == y("node_8")
    assert y("node_9") != y("node_12")

    # node_12 -> node_18 spans the ranks occupied by node_9/10/11.  Its two
    # endpoints share y=0 while those unrelated nodes are in another lane, so
    # the edge cannot visually masquerade as a parent edge into node_9.
    assert y("node_12") == y("node_18")
    assert x("node_12") < x("node_9") < x("node_18")
