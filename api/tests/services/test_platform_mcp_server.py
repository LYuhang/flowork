"""Renderer MCP dispatch and shared live Agent context security boundaries."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import types

from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.capability import agent_capability_policy
from vibecanvas_api.services.platform_mcp import invocation as platform_invocation
from vibecanvas_api.services.platform_mcp.catalog import BUILTIN_MCP_METADATA
from vibecanvas_api.services.platform_mcp.invocation import _tool_error_result
from vibecanvas_api.storage.sync_session import current_sync_tenant_id


def _capability(**overrides):
    values = dict(tenant_id="tenant-a", organization_id="tenant-a", user_id="user-a",
        chat_id="chat-a", turn_id="turn-a", workspace_scope_id="workspace-a",
        runtime_session_id="runtime-a", session_id="session-a", session_generation=3,
        membership_id="membership-a", authorization_generation="generation-a",
        approval_mode="agent", server="interactive")
    values.update(overrides)
    if "actions" not in values:
        values["actions"] = agent_capability_policy(organization_id=values["organization_id"],
            chat_id=values["chat_id"], workspace_scope_id=values["workspace_scope_id"], server=values["server"]).actions
    return SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["render_interactive", "render_url_preview", "get_workflow", "update_canvas"])
async def test_retired_names_are_not_callable_hidden_aliases(monkeypatch, name):
    monkeypatch.setattr(platform_invocation, "verify_agent_capability", lambda *_a, **_k: _capability())
    invoke = AsyncMock()
    monkeypatch.setattr(platform_invocation, "invoke_platform_mcp_tool_with_capability", invoke)
    with pytest.raises(ValueError, match="unknown interactive tool"):
        await platform_invocation.invoke_platform_mcp_tool(server="interactive", tool_name=name,
            arguments={}, capability_token="valid")
    invoke.assert_not_awaited()


def test_unified_preview_retains_read_only_source_ceiling():
    required = platform_invocation._required_tool_actions("interactive", "render_preview")
    assert {"vfs_path:view", "interactive_artifact:create", "workflow:view", "workflow:use"} <= required
    assert not {"workflow:update", "workflow:execute"} & required


def test_unavailable_workflow_does_not_invite_unchanged_preview_retry():
    result = _tool_error_result(code="workflow_unavailable", message="Unavailable", tool_name="render_preview")
    body = json.loads(result.content[0].text)
    assert result.isError
    assert "Do not repeat the same preview" in body["retry"]["instruction"]


def test_tool_error_result_is_model_correctable_mcp_error():
    result = _tool_error_result(code="invalid_tool_arguments", message="Unsupported renderer.",
        tool_name="render_preview", json_pointer="/type")
    assert result.isError and len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["error"] == {"code": "invalid_tool_arguments", "message": "Unsupported renderer.", "json_pointer": "/type"}
    assert payload["retry"]["tool"] == "render_preview"
    assert "published inputSchema" in payload["retry"]["instruction"]


def test_management_catalog_matches_only_the_renderer_boundary():
    assert set(BUILTIN_MCP_METADATA) == {"interactive"}
    entry = platform_invocation.platform_mcp_catalog_entry("interactive")
    manifest = {tool.name: tool for tool in platform_invocation.platform_mcp_tool_manifest("interactive")}
    assert {tool["name"] for tool in entry["tools"]} == set(manifest) == {"render_preview", "render_choices"}
    assert entry["activation_mode"] == "base" and entry["runtime_types"] == ["codex"]
    for tool in entry["tools"]:
        assert tool["input_schema"] == manifest[tool["name"]].inputSchema
        assert tool["input_schema"]["type"] == "object"


@pytest.mark.parametrize("server", ["config", "workflow", "build", "task", "deployment", "knowledge", "diagram", "document", "browser"])
def test_business_servers_are_removed_not_empty_manifests(server):
    with pytest.raises(ValueError, match="unknown platform MCP server"):
        platform_invocation.platform_mcp_tool_manifest(server)
    with pytest.raises(ValueError, match="unknown platform MCP server"):
        platform_invocation.platform_mcp_catalog_entry(server)


@pytest.mark.asyncio
async def test_gateway_verifies_and_invokes_published_renderer_without_http_context(monkeypatch):
    capability = _capability()
    tool = next(tool for tool in platform_invocation.platform_mcp_tool_implementations("interactive") if tool.name == "render_preview")
    invoke = AsyncMock(return_value=([types.TextContent(type="text", text="ok")], {"status": "success"}))
    verify = Mock(return_value=capability)
    monkeypatch.setattr(platform_invocation, "verify_agent_capability", verify)
    monkeypatch.setattr(platform_invocation, "invoke_platform_mcp_tool_with_capability", invoke)
    arguments = {"type": "url", "source": "https://example.com"}
    result = await platform_invocation.invoke_platform_mcp_tool(server="interactive", tool_name="render_preview",
        arguments=arguments, capability_token="turn-capability")
    assert result[1] == {"status": "success"}
    verify.assert_called_once_with("turn-capability", secret=platform_invocation.config.signing_secret, server="interactive")
    invoke.assert_awaited_once_with(tool, arguments, "interactive", capability)


@pytest.mark.asyncio
async def test_gateway_rejects_bad_capability_and_schema_before_invoking(monkeypatch):
    invoke = AsyncMock()
    monkeypatch.setattr(platform_invocation, "invoke_platform_mcp_tool_with_capability", invoke)
    monkeypatch.setattr(platform_invocation, "verify_agent_capability", lambda *_args, **_kwargs: None)
    with pytest.raises(PermissionError, match="invalid or expired"):
        await platform_invocation.invoke_platform_mcp_tool(server="interactive", tool_name="render_preview",
            arguments={"type": "file", "source": "/data/report.pdf"}, capability_token="invalid")
    monkeypatch.setattr(platform_invocation, "verify_agent_capability", lambda *_args, **_kwargs: _capability())
    result = await platform_invocation.invoke_platform_mcp_tool(server="interactive", tool_name="render_preview",
        arguments={}, capability_token="valid")
    assert isinstance(result, types.CallToolResult) and result.isError
    assert json.loads(result.content[0].text)["error"]["code"] == "invalid_tool_arguments"
    invoke.assert_not_awaited()


@pytest.mark.parametrize("tool", ["render_preview", "render_choices"])
def test_each_renderer_requires_its_signed_action_ceiling(tool):
    cap = _capability()
    platform_invocation._require_tool_capability(cap, server="interactive", tool_name=tool)
    for removed in platform_invocation._required_tool_actions("interactive", tool):
        incomplete = _capability(actions=tuple(action for action in cap.actions if action != removed))
        with pytest.raises(PermissionError, match="does not permit"):
            platform_invocation._require_tool_capability(incomplete, server="interactive", tool_name=tool)
    for private_scope in ("cli", "browser"):
        with pytest.raises(PermissionError, match="does not permit"):
            platform_invocation._require_tool_capability(_capability(server=private_scope), server="interactive", tool_name=tool)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "tool_error", "exception"])
async def test_renderer_invocation_scopes_and_restores_sync_tenant(monkeypatch, outcome):
    capability = _capability()
    context = SimpleNamespace()
    async def fake_context(_capability):
        assert current_sync_tenant_id.get() == "tenant-a"
        return context
    async def coroutine(*, runtime):
        assert runtime.context is context
        assert current_sync_tenant_id.get() == "tenant-a"
        if outcome == "exception":
            raise RuntimeError("renderer failed")
        return "preview result", {"status": "error" if outcome == "tool_error" else "success", "meta": {"tool": "render_preview"}}
    monkeypatch.setattr(agent_context, "resolve_context", fake_context)
    tool = SimpleNamespace(name="render_preview", coroutine=coroutine)
    outer = current_sync_tenant_id.set("outer-tenant")
    try:
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="renderer failed"):
                await platform_invocation.invoke_platform_mcp_tool_with_capability(tool, {}, "interactive", capability)
        else:
            result = await platform_invocation.invoke_platform_mcp_tool_with_capability(tool, {}, "interactive", capability)
            if outcome == "tool_error":
                assert isinstance(result, types.CallToolResult) and result.isError
            else:
                assert result[0][0].text == "preview result" and result[1]["status"] == "success"
        assert current_sync_tenant_id.get() == "outer-tenant"
    finally:
        current_sync_tenant_id.reset(outer)


def _identity():
    return AuthContext(
        user_id="user-a",
        tenant_id="tenant-a",
        email="user-a@example.com",
        active_organization_id="tenant-a",
        membership_id="membership-a",
        membership_role="member",
        membership_status="active",
        session_id="session-a",
        session_generation=3,
        authentication_strength="password",
    )


def _allow_context_checks(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_context,
        "resolve_identity",
        AsyncMock(return_value=_identity()),
    )

    class AllowService:
        async def check(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=True)

    monkeypatch.setattr(
        agent_context,
        "authz_service_for_session",
        lambda **_kwargs: AllowService(),
    )


    monkeypatch.setattr(agent_context, "scope_authz_service", lambda service, **_kwargs: service)


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", ["waiting_approval", "cancel_requested", "completed"])
async def test_platform_context_rejects_non_running_turn(
    monkeypatch, run_status: str
) -> None:
    capability = _capability()

    @asynccontextmanager
    async def fake_session_scope(**_kwargs):
        yield object()

    class FakeRunsRepo:
        def __init__(self, _session):
            pass

        async def get_for_chat(self, *_args, **_kwargs):
            return SimpleNamespace(status=run_status)

    monkeypatch.setattr(agent_context, "session_scope", fake_session_scope)
    monkeypatch.setattr(agent_context, "AgentRunsRepo", FakeRunsRepo)
    _allow_context_checks(monkeypatch)

    with pytest.raises(PermissionError, match="active run"):
        await agent_context.resolve_context(capability)


@pytest.mark.asyncio
async def test_platform_context_rejects_workspace_scope_mismatch(monkeypatch) -> None:
    capability = _capability(workspace_scope_id="workspace-from-token")

    @asynccontextmanager
    async def fake_session_scope(**_kwargs):
        yield object()

    class FakeRunsRepo:
        def __init__(self, _session):
            pass

        async def get_for_chat(self, *_args, **_kwargs):
            return SimpleNamespace(status="running")

    class FakeChatRepo:
        def __init__(self, _session, _user_id):
            pass

        async def get_platform_context_binding(self, _chat_id):
            return {
                "carrier_scope_id": "carrier-from-database",
                "runtime_session_id": "runtime-a",
                "current_workflow_id": None,
            }

    monkeypatch.setattr(agent_context, "session_scope", fake_session_scope)
    monkeypatch.setattr(agent_context, "AgentRunsRepo", FakeRunsRepo)
    monkeypatch.setattr(agent_context, "ChatRepo", FakeChatRepo)
    _allow_context_checks(monkeypatch)
    monkeypatch.setattr(
        agent_context,
        "chat_workspace_scope_id",
        lambda _chat_id: "workspace-from-database",
    )

    with pytest.raises(PermissionError, match="workspace"):
        await agent_context.resolve_context(capability)


@pytest.mark.asyncio
async def test_platform_context_rebuilds_active_membership_without_preloading_workflow(
    monkeypatch,
) -> None:
    capability = _capability()

    @asynccontextmanager
    async def fake_session_scope(**_kwargs):
        yield object()

    class FakeRunsRepo:
        def __init__(self, _session):
            pass

        async def get_for_chat(self, *_args, **_kwargs):
            return SimpleNamespace(status="running")

    class FakeChatRepo:
        def __init__(self, _session, _user_id):
            pass

        async def get_platform_context_binding(self, _chat_id):
            return {
                "carrier_scope_id": "carrier-a",
                "runtime_session_id": "runtime-a",
                "current_workflow_id": "workflow-a",
            }

    class FakeWorkflowRepo:
        def __init__(self, _user_id):
            pass

        def get_current_workflow(self, _workflow_id):
            raise AssertionError("workflow content was loaded before authorization")

    monkeypatch.setattr(agent_context, "session_scope", fake_session_scope)
    monkeypatch.setattr(agent_context, "AgentRunsRepo", FakeRunsRepo)
    monkeypatch.setattr(agent_context, "ChatRepo", FakeChatRepo)
    _allow_context_checks(monkeypatch)
    monkeypatch.setattr(agent_context, "SyncWorkflowRepo", FakeWorkflowRepo)
    monkeypatch.setattr(
        agent_context,
        "chat_workspace_scope_id",
        lambda _chat_id: "workspace-a",
    )

    context = await agent_context.resolve_context(capability)

    assert context.workflow == {}
    assert not hasattr(context, "current_workflow_id")
    assert context.authorization_membership_id == "membership-a"
    assert context.authorization_membership_role == "member"
    assert context.authorization_membership_status == "active"
    assert context.authorization_session_id == "session-a"
    assert context.authorization_session_generation == 3
    assert context.runtime_session_id == "runtime-a"
