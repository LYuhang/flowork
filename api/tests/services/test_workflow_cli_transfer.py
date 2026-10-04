"""Transfer transaction contracts. Written for the deferred workflow suite."""
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources import workflow_transfer as transfer


@pytest.fixture
def state(monkeypatch):
    ctx = SimpleNamespace(tenant_id="tenant", username="user", )
    session = SimpleNamespace(get=AsyncMock(return_value=object()))
    committed = []
    @asynccontextmanager
    async def scope(**kwargs):
        yield session
        committed.append(True)
    repo = SimpleNamespace(
        get_meta=AsyncMock(return_value={"workflow_name": "Current", "active_major": 2, "active_sub": 5}),
        get_workflow_at=AsyncMock(return_value={}),
        commit=AsyncMock(return_value=SimpleNamespace(parent_v=2, sv=6)),
    )
    read = AsyncMock()
    decision = AsyncMock()
    fence = AsyncMock()
    validate = AsyncMock(return_value=[])
    binding = AsyncMock()
    monkeypatch.setattr(transfer, "session_scope", scope)
    monkeypatch.setattr(transfer, "WorkflowRepo", lambda *args: repo)
    monkeypatch.setattr(transfer, "_require_workflow_read", read)
    async def selection(actual_session, actual_ctx, workflow_id, major, *, for_update=False):
        assert workflow_id == "live" and major == "v2"
        await binding(actual_session, actual_ctx, for_update=for_update)
        await read(actual_session, actual_ctx, workflow_id)
        return {"id": workflow_id, "major": 2, "sub": 5, "meta": repo.get_meta.return_value}
    monkeypatch.setattr(transfer, "resolve_target", selection)
    monkeypatch.setattr(transfer, "_require_active_chat_write", fence)
    monkeypatch.setattr(transfer, "_service", lambda *args: object())
    monkeypatch.setattr(transfer, "_workflow_resource", lambda *args: object())
    monkeypatch.setattr(transfer, "_workflow_decision", decision)
    monkeypatch.setattr(transfer, "validate_workflow_for_context", validate)
    monkeypatch.setattr(transfer, "collect_workflow_warnings", lambda graph: [])
    monkeypatch.setattr(transfer, "_auto_tidy_workflow", Mock())
    return SimpleNamespace(ctx=ctx, session=session, repo=repo, binding=binding, read=read,
                           decision=decision, fence=fence, validate=validate, committed=committed)


@pytest.mark.asyncio
async def test_download_uses_fresh_binding_and_one_immutable_version(state):
    result = await transfer.download_workflow(state.ctx, workflow_id="live", major="v2")
    assert result["id"] == "live"
    assert result["version"] == "v2.sv5"
    state.repo.get_workflow_at.assert_awaited_once_with("live", 2, 5)
    assert result["workflow"]["__meta__"]["workflow_subversion"] == 5
    state.fence.assert_not_awaited()
    state.repo.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_passes_explicit_base_version_and_keeps_identity_and_history(state):
    graph = {"__meta__": {"workflow_id": "live", "workflow_version": 1, "workflow_subversion": 0, "tags": ["ignored"]}, "start": {"node_type": "StartNode"}}
    original = deepcopy(graph)
    result = await transfer.upload_workflow(state.ctx, workflow_id="live", major="v2", expected_version="v2.sv5", workflow= graph, note="Change")
    assert result == {"id": "live", "version": "v2.sv6", "node_count": 1}
    assert graph == original
    assert state.committed == [True]
    state.binding.assert_awaited_once_with(state.session, state.ctx, for_update=True)
    state.fence.assert_awaited_once_with(state.session, state.ctx)
    assert state.decision.await_args.kwargs["action"] == transfer.Action.UPDATE
    args = state.repo.commit.await_args
    assert args.args[0] == "live"
    assert "tags" not in args.args[1]["__meta__"]
    assert args.kwargs == {"note": "Change", "stamp_metadata": True, "target_major": 2, "expected_version": "v2.sv5"}


@pytest.mark.asyncio
async def test_upload_wrong_workflow_never_commits(state):
    with pytest.raises(ToolError, match="workflow_mismatch"):
        await transfer.upload_workflow(state.ctx, workflow_id="live", major="v2", expected_version="v2.sv5", workflow= {"__meta__": {"workflow_id": "other"}})
    state.repo.commit.assert_not_awaited()
    assert not state.committed


@pytest.mark.asyncio
async def test_upload_invalid_graph_never_commits(state):
    state.validate.return_value = [{"node_id": "bad", "message": "Missing node config"}]
    with pytest.raises(ToolError, match="invalid_workflow"):
        await transfer.upload_workflow(state.ctx, workflow_id="live", major="v2", expected_version="v2.sv5", workflow= {})
    state.fence.assert_not_awaited()
    state.repo.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["read", "decision", "fence"])
async def test_upload_denied_or_inactive_turn_never_commits(state, guard):
    getattr(state, guard).side_effect = ToolError("permission_denied", "Denied")
    with pytest.raises(ToolError):
        await transfer.upload_workflow(state.ctx, workflow_id="live", major="v2", expected_version="v2.sv5", workflow= {})
    state.repo.commit.assert_not_awaited()
    assert not state.committed


@pytest.mark.asyncio
async def test_preview_pins_explicit_version_without_changing_binding(state):
    result = await transfer.read_workflow_snapshot(state.ctx, workflow_id="other", version="v1.sv2")
    assert result["version"] == "v1.sv2"
    state.binding.assert_not_awaited()
    state.read.assert_awaited_once_with(state.session, state.ctx, "other")
    state.repo.get_workflow_at.assert_awaited_once_with("other", 1, 2)
    state.repo.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_distinguishes_missing_version_from_empty_draft(state):
    assert (await transfer.read_workflow_snapshot(state.ctx, workflow_id="live"))["node_count"] == 0
    state.session.get.return_value = None
    with pytest.raises(ToolError, match="version_not_found"):
        await transfer.read_workflow_snapshot(state.ctx, workflow_id="live", version="v8.sv4")


@pytest.mark.asyncio
async def test_upload_refreshes_skill_name_without_changing_reference_or_input(state, monkeypatch):
    from uuid import uuid4
    from vibecanvas_api.services import workflow_resources
    identifier = str(uuid4())
    graph = {'worker': {'node_type': 'SubAgentNode', 'node_config': {
        'skills': [{'id': identifier, 'name': 'old name'}]}}}
    original = deepcopy(graph)
    from vibecanvas_api.authorization.types import PrincipalRef, PrincipalType
    monkeypatch.setattr(transfer, '_principal', lambda ctx: PrincipalRef(PrincipalType.USER, 'user'))
    monkeypatch.setattr('vibecanvas_api.services.runtime_skills.authorized_skill_rows',
                        AsyncMock(return_value=[{'skill_id': identifier, 'name': 'current name'}]))
    monkeypatch.setattr(transfer, '_request_context', lambda *args, **kwargs:
                        SimpleNamespace(active_organization_id='tenant'))
    monkeypatch.setattr(transfer, '_service', lambda *args:
                        SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True))))
    monkeypatch.setattr(workflow_resources, 'SkillsRepo', lambda session:
                        SimpleNamespace(get=AsyncMock(return_value={'name': 'current name'})))
    await transfer.upload_workflow(state.ctx, graph, workflow_id='live', major='v2', expected_version='v2.sv5')
    saved = state.repo.commit.await_args.args[1]
    assert saved['worker']['node_config']['skills'] == [{'id': identifier, 'name': 'current name'}]
    assert graph == original
