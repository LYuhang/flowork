"""Layout writes positions only, on an authorized and locked branch tip."""
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.types import Action
from vibecanvas_api.services.agent_resources import workflow_layout as layout


@pytest.fixture
def setup(monkeypatch):
    ctx = SimpleNamespace(tenant_id="tenant", username="user")
    session = object()

    @asynccontextmanager
    async def scope(**kwargs):
        assert kwargs == {"tenant_id": "tenant", "user_id": "user"}
        yield session

    graph = {"__meta__": {"custom": True},
             "node_1": {"node_type": "StartNode", "children": ["node_2"],
                        "node_config": {}, "__attributes__": {"x": 99, "y": 88, "color": "red"}},
             "node_2": {"node_type": "EndNode", "children": [], "node_config": {"outputs": {}}}}
    repo = SimpleNamespace(get_workflow_at=AsyncMock(return_value=graph),
                           commit=AsyncMock(return_value=SimpleNamespace(parent_v=2, sv=5)))
    active, decision = AsyncMock(), AsyncMock()
    target = AsyncMock(return_value={"major": 2, "sub": 4})
    monkeypatch.setattr(layout, "session_scope", scope)
    monkeypatch.setattr(layout, "WorkflowRepo", lambda *_: repo)
    monkeypatch.setattr(layout, "_require_active_chat_write", active)
    monkeypatch.setattr(layout, "resolve_target", target)
    monkeypatch.setattr(layout, "_decision", decision)
    monkeypatch.setattr(layout, "_service", lambda *_: None)
    monkeypatch.setattr(layout, "_workflow_resource", lambda *_: None)
    return ctx, session, graph, repo, active, decision, target


@pytest.mark.asyncio
async def test_layout_locks_selected_tip_and_preserves_graph(setup):
    ctx, session, graph, repo, active, decision, target = setup
    before = deepcopy(graph)
    result = await layout.layout_workflow(ctx, workflow_id="wf", major="v2")
    assert result["changed"] is True and result["moved_nodes"] == 2
    assert result["version"] == "v2.sv5"
    active.assert_awaited_once_with(session, ctx)
    target.assert_awaited_once_with(session, ctx, "wf", "v2", for_update=True)
    assert decision.await_args.kwargs["action"] == Action.UPDATE
    repo.get_workflow_at.assert_awaited_once_with("wf", 2, 4)
    assert repo.commit.await_args.kwargs["target_major"] == 2
    assert repo.commit.await_args.kwargs["stamp_metadata"] is True
    saved = deepcopy(repo.commit.await_args.args[1])
    assert saved["node_1"]["__attributes__"]["color"] == "red"
    assert saved["node_2"]["__attributes__"]["x"] > saved["node_1"]["__attributes__"]["x"]
    for key in ("node_1", "node_2"):
        if "__attributes__" in before[key]:
            saved[key]["__attributes__"] = before[key]["__attributes__"]
        else:
            saved[key].pop("__attributes__")
    assert saved == before == graph


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_tidy_or_empty_does_not_create_version(setup, empty):
    ctx, _, graph, repo, _, decision, _ = setup
    if empty:
        graph.clear()
    else:
        layout._auto_tidy_workflow(graph)
    result = await layout.layout_workflow(ctx, workflow_id="wf", major="v2")
    assert result["changed"] is False and result["moved_nodes"] == 0
    assert result["version"] == "v2.sv4"
    repo.commit.assert_not_awaited()
    decision.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["active", "target", "permission"])
async def test_rejected_requests_never_read_or_commit_graph(setup, stage):
    ctx, _, _, repo, active, decision, target = setup
    {"active": active, "target": target, "permission": decision}[stage].side_effect = ToolError("denied", "Unavailable.")
    with pytest.raises(ToolError):
        await layout.layout_workflow(ctx, workflow_id="wf", major="v2")
    repo.get_workflow_at.assert_not_awaited()
    repo.commit.assert_not_awaited()
