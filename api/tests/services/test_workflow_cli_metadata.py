"""Live binding and metadata PATCH contracts; run with the deferred workflow suite."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources import authorization as auth


@pytest.fixture
def state(monkeypatch):
    ctx = SimpleNamespace(tenant_id="tenant", username="user", chat_id="chat",
                          runtime_session_id="runtime", )
    session = object()
    meta = {"wf_id": "live-id", "workflow_name": "Live", "description": "Private", "tags": ["old"], "active_major": 3, "active_sub": 4}
    repo = SimpleNamespace(get_meta=AsyncMock(return_value=meta), update_meta=AsyncMock(return_value=meta), max_subversion=AsyncMock(return_value=4))
    decisions = AsyncMock()
    fence = AsyncMock()

    @asynccontextmanager
    async def scope(**kwargs):
        assert kwargs == {"tenant_id": "tenant"}
        yield session

    monkeypatch.setattr(auth, "session_scope", scope)
    monkeypatch.setattr(auth, "WorkflowRepo", lambda *_args: repo)
    monkeypatch.setattr(auth, "_service", lambda *_args: object())
    monkeypatch.setattr(auth, "_decision", decisions)
    monkeypatch.setattr(auth, "_require_active_chat_write", fence)
    return ctx, session, repo, decisions, fence


@pytest.mark.asyncio
async def test_get_is_explicit_read_only_metadata(state):
    ctx, session, repo, decisions, fence = state
    assert await auth.get_authorized_workflow_metadata(ctx, "live-id") == repo.get_meta.return_value
    repo.get_meta.assert_awaited_once_with("live-id", for_update=False)
    assert [c.kwargs["action"] for c in decisions.await_args_list] == [auth.Action.USE, auth.Action.VIEW]
    fence.assert_not_awaited()
    repo.update_meta.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["permission_denied", "authorization_unavailable"])
async def test_read_distinguishes_denial_and_outage(state, code):
    ctx, _, repo, decisions, _ = state
    decisions.side_effect = ToolError(code, "Unavailable")
    with pytest.raises(ToolError, match="workflow_unavailable" if code == "permission_denied" else code):
        await auth.get_authorized_workflow_metadata(ctx, "live-id")
    repo.get_meta.assert_not_awaited()

@pytest.mark.asyncio
async def test_deleted_workflow_unavailable(state):
    ctx, _, repo, _, _ = state
    repo.get_meta.return_value = {}
    with pytest.raises(ToolError, match="workflow_unavailable"):
        await auth.get_authorized_workflow_metadata(ctx, "live-id")

@pytest.mark.asyncio
async def test_update_patches_only_explicit_fields(state):
    ctx, session, repo, decisions, fence = state
    await auth.get_authorized_workflow_metadata(ctx, "live-id", {"description": "", "tags": ["a", "b"]})
    fence.assert_awaited_once_with(session, ctx)
    repo.update_meta.assert_awaited_once_with("live-id", description="", tags=["a", "b"])
    assert [c.kwargs["action"] for c in decisions.await_args_list] == [auth.Action.USE, auth.Action.VIEW, auth.Action.UPDATE]

@pytest.mark.asyncio
async def test_update_denied_never_writes(state):
    ctx, _, repo, decisions, _ = state
    decisions.side_effect = [None, None, ToolError("permission_denied", "Denied")]
    with pytest.raises(ToolError, match="permission_denied"):
        await auth.get_authorized_workflow_metadata(ctx, "live-id", {"name": "New"})
    repo.update_meta.assert_not_awaited()

@pytest.mark.asyncio
async def test_ended_run_cannot_update(state):
    ctx, _, repo, _, fence = state
    fence.side_effect = ToolError("runtime_unavailable", "Ended")
    with pytest.raises(ToolError, match="runtime_unavailable"):
        await auth.get_authorized_workflow_metadata(ctx, "live-id", {"name": "New"})
    repo.update_meta.assert_not_awaited()
