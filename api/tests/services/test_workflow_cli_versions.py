"""Explicit branch isolation, with no Chat selection state."""
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources import authorization as auth
from vibecanvas_api.services.agent_resources import workflow_transfer as transfer
from vibecanvas_api.services.agent_resources import workflow_versions as versions
from vibecanvas_api.services.agent_resources import workflow_target as target

@pytest.fixture
def state(monkeypatch):
    graphs = {(1, 0): {}, (1, 8): {"one": {}}, (2, 0): {}, (2, 3): {"two": {}}, (3, 0): {}}
    meta = {"wf_id": "wf", "workflow_name": "Shared", "active_major": 3, "active_sub": 0}
    session = SimpleNamespace(get=AsyncMock(return_value=object()))
    saves = []

    @asynccontextmanager
    async def scope(**kwargs):
        before = deepcopy((graphs, meta))
        try:
            yield session
        except BaseException:
            for current, old in zip((graphs, meta), before):
                current.clear()
                current.update(old)
            raise

    async def max_sub(wf_id, major):
        return max((sub for v, sub in graphs if v == major), default=-1)

    async def majors(wf_id):
        return [{"v": v, "sv": await max_sub(wf_id, v)} for v in sorted({v for v, _ in graphs})]

    async def new_version(wf_id, graph, *, note, stamp_metadata, source_version):
        assert stamp_metadata and graph == graphs[source_version]
        major = max(v for v, _ in graphs) + 1
        graphs[major, 0] = deepcopy(graph)
        meta.update(active_major=major, active_sub=0)
        return major

    async def commit(wf_id, graph, *, note, stamp_metadata, target_major, expected_version):
        assert stamp_metadata
        assert expected_version == f"v{target_major}.sv{await max_sub(wf_id, target_major)}"
        sub = await max_sub(wf_id, target_major) + 1
        graphs[target_major, sub] = deepcopy(graph)
        meta.update(active_major=target_major, active_sub=sub)
        return SimpleNamespace(parent_v=target_major, sv=sub)

    repo = SimpleNamespace(
        get_meta=AsyncMock(side_effect=lambda *_a, **_kw: dict(meta)),
        max_subversion=AsyncMock(side_effect=max_sub),
        list_major_versions=AsyncMock(side_effect=majors),
        get_workflow_at=AsyncMock(side_effect=lambda wf_id, major, sub: deepcopy(graphs[major, sub])),
        new_version=AsyncMock(side_effect=new_version), commit=AsyncMock(side_effect=commit),
        checkout_major=AsyncMock(),
    )
    decision, fence = AsyncMock(), AsyncMock()
    monkeypatch.setattr(target, "WorkflowRepo", lambda *_: repo)
    monkeypatch.setattr(target, "_require_workflow_read", AsyncMock())
    for module in (auth, transfer, versions):
        monkeypatch.setattr(module, "session_scope", scope)
        monkeypatch.setattr(module, "WorkflowRepo", lambda *_: repo)
        monkeypatch.setattr(module, "_workflow_decision", decision)
        monkeypatch.setattr(module, "_require_active_chat_write", fence)
    for module in (auth, transfer):
        monkeypatch.setattr(module, "_service", lambda *_: object())
    monkeypatch.setattr(transfer, "validate_workflow_for_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(transfer, "collect_workflow_warnings", lambda _: [])
    monkeypatch.setattr(transfer, "_auto_tidy_workflow", lambda _: None)
    ctx = SimpleNamespace(tenant_id="tenant", username="user", chat_id="chat", runtime_session_id="runtime")
    return SimpleNamespace(graphs=graphs, meta=meta, repo=repo, ctx=ctx,
                           decision=decision, fence=fence)


@pytest.mark.asyncio
async def test_explicit_branch_overrides_head_and_previous_command(state):
    ctx = state.ctx
    before = dict(vars(ctx))
    assert (await transfer.download_workflow(ctx, workflow_id="wf", major="v2"))["version"] == "v2.sv3"
    assert (await transfer.download_workflow(ctx, workflow_id="wf", major="v1"))["version"] == "v1.sv8"
    source = {"__meta__": {"workflow_id": "wf", "workflow_version": 99}, "edited": {}}
    assert (await transfer.upload_workflow(ctx, source, workflow_id="wf", major="v2", expected_version="v2.sv3"))["version"] == "v2.sv4"
    assert (await transfer.download_workflow(ctx, workflow_id="wf", major="v1"))["version"] == "v1.sv8"
    assert source["__meta__"]["workflow_version"] == 99
    assert vars(ctx) == before
    state.repo.checkout_major.assert_not_awaited()

@pytest.mark.asyncio
async def test_create_branches_from_explicit_tip(state):
    result = await versions.workflow_version_command(state.ctx, "workflow.version.create",
        {"workflow_id": "wf", "major": "v1", "note": "Milestone"})
    assert result == {"id": "wf", "previous_version": "v1.sv8", "version": "v4.sv0"}
    assert state.repo.new_version.await_args.kwargs["source_version"] == (1, 8)
    assert state.graphs[4, 0] == {"one": {}}
    assert state.meta["active_major"] == 4

@pytest.mark.asyncio
async def test_list_reports_head_and_all_branches_without_mutation(state):
    before = deepcopy((state.graphs, state.meta, vars(state.ctx)))
    result = await versions.workflow_version_command(state.ctx, "workflow.version.list", {"workflow_id": "wf"})
    assert result["version"] == "v3.sv0"
    assert result["versions"] == [{"major": 1, "version": "v1.sv8"}, {"major": 2, "version": "v2.sv3"}, {"major": 3, "version": "v3.sv0"}]
    assert (state.graphs, state.meta, vars(state.ctx)) == before

@pytest.mark.asyncio
async def test_missing_major_does_not_fall_back_to_head(state):
    with pytest.raises(ToolError, match="version_not_found"):
        await transfer.download_workflow(state.ctx, workflow_id="wf", major="v99")
    state.repo.get_workflow_at.assert_not_awaited()

@pytest.mark.asyncio
async def test_denied_write_does_not_allocate_version(state):
    state.decision.side_effect = ToolError("permission_denied", "Denied")
    with pytest.raises(ToolError, match="permission_denied"):
        await versions.workflow_version_command(state.ctx, "workflow.version.create",
            {"workflow_id": "wf", "major": "v1", "note": ""})
    state.repo.new_version.assert_not_awaited()

@pytest.mark.asyncio
async def test_commit_failure_rolls_back_saved_version(state):
    before = deepcopy((state.graphs, state.meta))
    state.repo.commit.side_effect = RuntimeError("database failure")
    with pytest.raises(RuntimeError):
        await transfer.upload_workflow(state.ctx, {}, workflow_id="wf", major="v2", expected_version="v2.sv3")
    assert (state.graphs, state.meta) == before
