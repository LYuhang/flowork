"""Transaction/lifecycle contracts; execution deferred with the workflow suite."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources import authorization as auth


@pytest.fixture
def transaction(monkeypatch):
    events = []
    session = SimpleNamespace(commit=AsyncMock(side_effect=lambda: events.append("commit")))
    ctx = SimpleNamespace(username=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()),
                          chat_id="chat", turn_id="turn", runtime_session_id="runtime")
    captured = {}

    @asynccontextmanager
    async def scope(**_kwargs):
        try:
            yield session
        except BaseException:
            events.append("rollback")
            raise

    class Repo:
        def __init__(self, actual_session, user):
            assert actual_session is session
            assert user == ctx.username

        async def create_workflow(self, **kwargs):
            captured.update(kwargs)
            events.append("create")
            return {"wf_id": kwargs["wf_id"], "workflow_name": kwargs["name"], "active_major": 1, "active_sub": 0}

    async def fence(actual_session, actual_ctx):
        assert actual_session is session and actual_ctx is ctx
        events.append("fence")

    async def enqueue(**kwargs):
        assert kwargs["session"] is session
        events.append("enqueue")
        return ["intent"]

    async def apply(*_args):
        events.append("apply")

    async def load(_ctx, wf_id, _action):
        return auth.AuthorizedWorkflowSnapshot({"wf_id": wf_id}, captured["initial_workflow"], SimpleNamespace())

    monkeypatch.setattr(auth, "session_scope", scope)
    monkeypatch.setattr(auth, "WorkflowRepo", Repo)
    monkeypatch.setattr(auth, "_service", lambda *_args: object())
    monkeypatch.setattr(auth, "_decision", AsyncMock())
    monkeypatch.setattr(auth, "_require_active_chat_write", fence)
    monkeypatch.setattr(auth, "AuthzMutationCoordinator", lambda **_kwargs: object())
    monkeypatch.setattr(auth, "enqueue_structural_delta", enqueue)
    monkeypatch.setattr(auth, "apply_committed_structural_mutations", apply)
    monkeypatch.setattr(auth, "load_authorized_workflow", load)
    return ctx, session, captured, events


@pytest.mark.asyncio
async def test_create_content_binding_and_auth_intent_commit_together(transaction):
    ctx, _, captured, events = transaction
    original = {"__meta__": {"workflow_id": "source", "workflow_version": 9}, "node": {"node_type": "StartNode"}}
    result = await auth.create_authorized_workflow(ctx, name="New", description="D", tags=["T"], initial_workflow=original)
    assert events == ["fence", "create", "enqueue", "commit", "apply"]
    assert result.meta["wf_id"] == captured["wf_id"] != "source"
    assert captured["tags"] == ["T"]
    assert captured["initial_workflow"]["__meta__"] == {
        "workflow_id": captured["wf_id"], "workflow_name": "New", "workflow_version": 1, "workflow_subversion": 0,
    }
    assert original["__meta__"]["workflow_id"] == "source"


@pytest.mark.asyncio
async def test_binding_failure_rolls_back_creation_before_commit(transaction, monkeypatch):
    ctx, session, _, events = transaction
    monkeypatch.setattr(auth, "_require_active_chat_write", AsyncMock(side_effect=ToolError("runtime_unavailable", "Run ended")))
    with pytest.raises(ToolError, match="runtime_unavailable"):
        await auth.create_authorized_workflow(ctx, name="New", description="")
    session.commit.assert_not_awaited()
    assert events == ["rollback"]


@pytest.mark.asyncio
async def test_projection_failure_reports_committed_resource(transaction, monkeypatch):
    ctx, _, captured, events = transaction
    monkeypatch.setattr(auth, "apply_committed_structural_mutations", AsyncMock(side_effect=RuntimeError("offline")))
    with pytest.raises(ToolError, match="authorization_pending") as exc:
        await auth.create_authorized_workflow(ctx, name="New", description="")
    assert "commit" in events
    assert exc.value.info == {"id": captured["wf_id"], "created": True, "authorization_ready": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,cancelled", [("completed", False), ("cancel_requested", True), ("running", True)])
async def test_ended_or_cancelling_run_cannot_write_binding(monkeypatch, status, cancelled):
    ctx = SimpleNamespace(username=str(uuid.uuid4()), chat_id="chat", turn_id="turn", runtime_session_id="runtime")
    run = SimpleNamespace(status=status, cancel_requested_at=object() if cancelled else None)
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run)))
    with pytest.raises(ToolError, match="runtime_unavailable"):
        await auth._require_active_chat_write(session, ctx)
