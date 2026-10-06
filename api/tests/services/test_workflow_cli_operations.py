"""Atomic operation-group persistence contracts; deferred with the workflow suite."""
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources import workflow_operations as ops


def graph():
    return {"a": {"node_id": "a", "node_type": "StartNode", "node_config": {"custom": {"keep": 1}}, "children": ["b"]},
            "b": {"node_id": "b", "node_type": "EndNode", "node_config": {}, "children": []}}


def test_remove_node_clears_edges_without_mutating_input():
    original = graph()
    result = ops.apply_operation(original, {"op": "node_remove", "node_id": "b"})
    assert "b" not in result and result["a"]["children"] == []
    assert original == graph()


def test_update_replaces_exact_value_and_decodes_pointer():
    original = graph()
    result = ops.apply_operation(original, {"op": "node_update", "path": "/a/node_config/custom/a~1b~0c", "value": [True, None]})
    assert result["a"]["node_config"]["custom"] == {"keep": 1, "a/b~c": [True, None]}
    replaced = ops.apply_operation(result, {"op": "node_update", "path": "/a/node_config/custom", "value": {}})
    assert replaced["a"]["node_config"]["custom"] == {} and original == graph()


def test_known_config_types_checked_but_missing_required_fields_allowed():
    draft = ops.apply_operation({}, {"op": "node_add", "node": {"node_id": "p", "node_type": "PromptNode"}})
    with pytest.raises(ToolError, match="invalid_value"):
        ops.apply_operation(draft, {"op": "node_update", "path": "/p/node_config/prompt_template", "value": 42})
    edited = ops.apply_operation(draft, {"op": "node_update", "path": "/p/node_config/prompt_template", "value": "Text"})
    assert edited["p"]["node_config"] == {"prompt_template": "Text"}
    assert draft["p"]["node_config"] == {}


@pytest.mark.parametrize("operation,code", [
    ({"op": "edge_add", "source": "a", "target": "b"}, "edge_exists"),
    ({"op": "edge_remove", "source": "b", "target": "a"}, "edge_not_found"),
    ({"op": "node_remove", "node_id": "missing"}, "node_not_found"),
    ({"op": "node_update", "path": "/a/children", "value": []}, "protected_field"),
    ({"op": "node_update", "path": "/a/node_id", "value": "new"}, "protected_field"),
    ({"op": "node_update", "path": "/__meta__/workflow_version", "value": 8}, "invalid_node_id"),
    ({"op": "node_update", "path": "/a/node_config/missing/field", "value": 1}, "path_not_found"),
    ({"op": "node_update", "path": "/a/node_config", "value": "wrong"}, "invalid_value"),
    ({"op": "node_add", "node": {"node_id": "a", "node_type": "StartNode"}}, "node_exists"),
])
def test_failed_step_is_atomic(operation, code):
    original = graph()
    before = deepcopy(original)
    with pytest.raises(ToolError, match=code):
        ops.apply_operation(original, operation)
    assert original == before


@pytest.fixture
def service(monkeypatch):
    ctx = SimpleNamespace(tenant_id="tenant", username="user")
    events = []
    @asynccontextmanager
    async def scope(**kwargs):
        yield object()
        events.append("committed")
    selection = AsyncMock(return_value={"id": "wf", "major": 2, "sub": 7})
    repo = SimpleNamespace(get_workflow_at=AsyncMock(return_value=graph()),
                           commit=AsyncMock(return_value=SimpleNamespace(parent_v=2, sv=8)))
    pointer = AsyncMock()
    monkeypatch.setattr(ops, "session_scope", scope)
    monkeypatch.setattr(ops, "_require_active_chat_write", AsyncMock())
    monkeypatch.setattr(ops, "resolve_target", selection)
    monkeypatch.setattr(ops, "_workflow_decision", AsyncMock())
    monkeypatch.setattr(ops, "_service", lambda *_: object())
    monkeypatch.setattr(ops, "WorkflowRepo", lambda *_: repo)
    return ctx, selection, repo, pointer, events


@pytest.mark.asyncio
async def test_later_failure_discards_entire_group_without_new_version(service):
    ctx, selection, repo, pointer, events = service
    result = await ops.operate_workflow(ctx, {"workflow_id": "wf", "major": "v2", "operations": [
        {"op": "edge_remove", "source": "a", "target": "b"},
        {"op": "node_remove", "node_id": "missing"},
        {"op": "node_remove", "node_id": "a"},
    ]})
    assert result["applied"] == 0 and result["skipped"] == 1 and result["failed_index"] == 1
    assert result["error"] == "node_not_found" and result["version"] == "v2.sv7"
    assert [item["status"] for item in result["results"]] == ["not_saved", "failed"]
    assert events == ["committed"]
    repo.commit.assert_not_awaited()
    assert repo.get_workflow_at.return_value == graph()
    assert result["failed_operation"] == "node_remove"
    assert "No operations were saved" in result["message"]
    assert selection.await_args.kwargs["for_update"] is True
    repo.get_workflow_at.assert_awaited_once_with("wf", 2, 7)
    pointer.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_successful_steps_share_one_new_subversion(service):
    ctx, _, repo, pointer, _ = service
    result = await ops.operate_workflow(ctx, {"workflow_id": "wf", "major": "v2", "operations": [
        {"op": "node_add", "node": {"node_id": "c", "node_type": "EndNode"}},
        {"op": "edge_remove", "source": "a", "target": "b"},
        {"op": "edge_add", "source": "a", "target": "c"},
    ], "note": "New path"})
    assert result["applied"] == result["total"] == 3 and result["skipped"] == 0
    assert "error" not in result and result["version"] == "v2.sv8"
    assert repo.commit.await_count == 1
    pointer.assert_not_awaited()
    assert repo.commit.await_args.args[1]["a"]["children"] == ["c"]
    assert repo.commit.await_args.kwargs["note"] == "New path"


@pytest.mark.asyncio
async def test_first_error_creates_no_version(service):
    ctx, _, repo, pointer, _ = service
    result = await ops.operate_workflow(ctx, {"workflow_id": "wf", "major": "v2", "operations": [{"op": "node_remove", "node_id": "missing"}]})
    assert result["applied"] == 0 and result["version"] == "v2.sv7"
    repo.commit.assert_not_awaited()
    pointer.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_binding_does_not_read_or_write_graph(service):
    ctx, selection, repo, pointer, _ = service
    selection.side_effect = ToolError("workflow_unavailable", "The workflow is unavailable.")
    with pytest.raises(ToolError, match="workflow_unavailable"):
        await ops.operate_workflow(ctx, {"workflow_id": "wf", "major": "v2", "operations": [{"op": "node_remove", "node_id": "a"}]})
    repo.get_workflow_at.assert_not_awaited()
    repo.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_failure_does_not_return_saved_feedback(service, monkeypatch):
    ctx, *_ = service
    @asynccontextmanager
    async def fail_commit(**kwargs):
        yield object()
        raise RuntimeError("Commit outcome unknown.")
    monkeypatch.setattr(ops, "session_scope", fail_commit)
    with pytest.raises(RuntimeError, match="Commit outcome unknown"):
        await ops.operate_workflow(ctx, {"workflow_id": "wf", "major": "v2", "operations": [{"op": "edge_remove", "source": "a", "target": "b"}]})


@pytest.mark.asyncio
async def test_retry_whole_group_after_failure_saves_once_and_allows_incomplete_draft(service):
    ctx, _, repo, _, _ = service
    operations = [
        {"op": "node_add", "node": {"node_id": "p", "node_type": "PromptNode"}},
        {"op": "node_update", "path": "/p/node_description", "value": "Unfinished draft"},
        {"op": "edge_add", "source": "p", "target": "missing"},
    ]
    result = await ops.operate_workflow(ctx, {"workflow_id": "wf", "major": "v2", "operations": operations})
    assert result["applied"] == 0 and result["failed_index"] == 2
    assert [item["status"] for item in result["results"]] == ["not_saved", "not_saved", "failed"]
    repo.commit.assert_not_awaited()
    operations[-1]["target"] = "b"
    result = await ops.operate_workflow(ctx, {"workflow_id": "wf", "major": "v2", "operations": operations})
    assert "error" not in result and result["applied"] == 3
    assert [item["status"] for item in result["results"]] == ["saved"] * 3
    repo.commit.assert_awaited_once()
    saved = repo.commit.await_args.args[1]
    assert saved["p"]["node_config"] == {}  # Missing required prompt config remains a valid draft edit.
    assert saved["p"]["children"] == ["b"]
    assert saved["p"]["node_description"] == "Unfinished draft"


@pytest.mark.asyncio
async def test_forward_child_reference_explains_atomic_construction_order(service):
    ctx, _, repo, _, _ = service
    result = await ops.operate_workflow(ctx, {'workflow_id': 'wf', 'major': 'v2', 'operations': [
        {'op': 'node_add', 'node': {'node_id': 'node_4', 'node_type': 'CodeNode', 'children': ['node_5']}},
        {'op': 'node_add', 'node': {'node_id': 'node_5', 'node_type': 'EndNode'}},
    ]})
    assert result['error'] == 'node_not_found'
    assert result['applied'] == 0
    assert 'children: []' in result['hint']
    assert 'same atomic group' in result['hint']
    repo.commit.assert_not_awaited()
