from types import SimpleNamespace

import pytest

from vibecanvas_api.agents.tools import builtin_tool_names
from vibecanvas_api.services.platform_mcp.build_tools._target import target_workflow_id
from vibecanvas_api.services.platform_mcp.build_tools import workflow_context
from vibecanvas_api.agents.tools.decorator import ToolError


def _names(tools):
    return [getattr(t, "name", "") for t in tools]


def test_workflow_context_tools_are_platform_mcp_only():
    from vibecanvas_api.services.platform_mcp.build_tools.workflow_context import (
        CHAT_WORKFLOW_CONTEXT_TOOLS,
    )

    private_names = builtin_tool_names()

    assert _names(CHAT_WORKFLOW_CONTEXT_TOOLS) == [
        "set_workflow",
        "create_workflow",
    ]
    for name in ("list_workflows", "set_workflow", "create_workflow"):
        assert name not in private_names


def test_chat_build_requires_real_current_workflow():
    ctx = SimpleNamespace(surface="chat", current_workflow_id=None, wf_id="__chat_user")

    with pytest.raises(ToolError) as exc:
        target_workflow_id(ctx)

    assert str(exc.value) == "no_workflow"
    assert "set_workflow" in (exc.value.message or "")


def test_build_tools_use_explicit_current_workflow():
    ctx = SimpleNamespace(surface="chat", current_workflow_id="wf_123", wf_id="__chat_user")

    assert target_workflow_id(ctx) == "wf_123"


def test_persist_current_workflow_keeps_chat_sandbox_attached(monkeypatch):
    calls = []

    class Repo:
        def __init__(self, user_id):
            self.user_id = user_id

        def set_current_workflow_id(self, chat_id, wf_id):
            calls.append((self.user_id, chat_id, wf_id))

    monkeypatch.setattr(workflow_context, "SyncChatRepo", Repo)
    attached_session = object()
    ctx = SimpleNamespace(
        chat_id="chat_1",
        username="user_1",
        current_workflow_id=None,
        _attached_session=attached_session,
    )

    workflow_context._persist_current_workflow(ctx, "workflow_1")

    assert calls == [("user_1", "chat_1", "workflow_1")]
    assert ctx.current_workflow_id == "workflow_1"
    # Workflow selection is durable Chat metadata. The resident Runtime is
    # scoped to the Chat workspace, so selecting another workflow must not
    # invalidate or restart that sandbox.
    assert ctx._attached_session is attached_session


@pytest.mark.asyncio
async def test_set_workflow_updates_context_without_restarting_chat_sandbox(monkeypatch):
    repo_calls = []

    class Repo:
        def __init__(self, user_id):
            self.user_id = user_id

        def set_current_workflow_id(self, chat_id, wf_id):
            repo_calls.append((self.user_id, chat_id, wf_id))

    class WorkflowRepo:
        def get_meta(self, wf_id):
            return {
                "wf_id": wf_id,
                "workflow_name": "Review Flow",
                "active_major": 1,
                "active_sub": 0,
            }

        def get_current_workflow(self, wf_id):
            return {"nodes": []}

    monkeypatch.setattr(workflow_context, "SyncChatRepo", Repo)
    async def fake_load(_ctx, wf_id, _action):
        return SimpleNamespace(
            meta={
                "wf_id": wf_id,
                "workflow_name": "Review Flow",
                "active_major": 1,
                "active_sub": 0,
            },
            workflow={"nodes": []},
        )

    async def fake_require(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        workflow_context,
        "load_authorized_workflow",
        fake_load,
    )
    monkeypatch.setattr(
        workflow_context,
        "recheck_platform_workflow_action",
        fake_require,
    )
    attached_session = object()
    ctx = SimpleNamespace(
        chat_id="chat_1",
        username="user_1",
        tenant_id="tenant_1",
        wf_id="__chatws_user_chat1",
        current_workflow_id=None,
        _attached_session=attached_session,
        repo=WorkflowRepo(),
        workflow=None,
    )

    content, artifact = await workflow_context._do_set_workflow(
        "workflow_1",
        SimpleNamespace(context=ctx),
    )

    assert "Current workflow changed to workflow_1" in content
    assert artifact["status"] == "success"
    assert repo_calls == [("user_1", "chat_1", "workflow_1")]
    assert ctx.current_workflow_id == "workflow_1"
    assert ctx._attached_session is attached_session
