from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import types

from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.services.platform_mcp import invocation as platform_invocation
from vibecanvas_api.services.platform_mcp.capability import platform_mcp_policy
from vibecanvas_api.services.platform_mcp.invocation import _tool_error_result
from vibecanvas_api.agents.prompts.diagram import DIAGRAM_MCP_TOOL_NAMES
from vibecanvas_api.services.platform_mcp.catalog import sandbox_mcp_catalog
from vibecanvas_api.storage.sync_session import current_sync_tenant_id


def _capability(**overrides):
    values = {
        "tenant_id": "tenant-a",
        "organization_id": "tenant-a",
        "user_id": "user-a",
        "chat_id": "chat-a",
        "turn_id": "turn-a",
        "workspace_scope_id": "workspace-a",
        "runtime_session_id": "runtime-a",
        "session_id": "session-a",
        "session_generation": 3,
        "membership_id": "membership-a",
        "authorization_generation": "generation-a",
        "approval_mode": "agent",
        "server": "workflow",
        "actions": (
            "chat:execute",
            "platform_mcp:call",
            "workflow:view",
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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
        platform_invocation,
        "_live_platform_identity",
        AsyncMock(return_value=_identity()),
    )

    class AllowService:
        async def check(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=True)

    monkeypatch.setattr(
        platform_invocation,
        "authz_service_for_session",
        lambda **_kwargs: AllowService(),
    )


async def _tools(server) -> dict[str, types.Tool]:
    if isinstance(server, str):
        return {
            tool.name: tool
            for tool in platform_invocation.platform_mcp_tool_manifest(server)
        }
    handler = server._mcp_server.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest(method="tools/list"))
    return {tool.name: tool for tool in result.root.tools}


def test_tool_error_result_is_model_correctable_mcp_error() -> None:
    result = _tool_error_result(
        code="invalid_tool_arguments",
        message="'ref' is a required property",
        tool_name="update_canvas",
        json_pointer="/ref",
    )

    assert result.isError is True
    assert len(result.content) == 1
    assert isinstance(result.content[0], types.TextContent)
    payload = json.loads(result.content[0].text)
    assert payload == {
        "status": "error",
        "error": {
            "code": "invalid_tool_arguments",
            "message": "'ref' is a required property",
            "json_pointer": "/ref",
        },
        "retry": {
            "tool": "update_canvas",
            "instruction": (
                "Correct only the rejected argument using this tool's "
                "published inputSchema and exact previously returned values."
            ),
        },
    }


@pytest.mark.asyncio
async def test_management_catalog_matches_each_runtime_boundary() -> None:
    """Platform and sandbox catalogs must each match their runtime boundary."""
    servers = {
        name: name
        for name in (
            "config",
            "interactive",
            "workflow",
            "task",
            "deployment",
            "knowledge",
            "build",
        )
    }
    platform_catalog = [
        {
            "id": name,
            **platform_invocation.platform_mcp_catalog_entry(name),
        }
        for name in servers
    ]
    sandbox_catalog = sandbox_mcp_catalog()
    catalog = {item["id"]: item for item in [*platform_catalog, *sandbox_catalog]}

    assert set(catalog) == {*servers, "diagram", "document"}
    assert {item["id"] for item in platform_catalog} == set(servers)
    assert [item["id"] for item in sandbox_catalog] == ["diagram", "document"]
    for server_name, server in servers.items():
        registered = await _tools(server)
        listed = {tool["name"]: tool for tool in catalog[server_name]["tools"]}
        assert set(listed) == set(registered)
        assert all(tool["input_schema"]["type"] == "object" for tool in listed.values())

    assert catalog["workflow"]["activation_mode"] == "command"
    assert catalog["workflow"]["activation"] == "/workflow"
    assert catalog["task"]["activation"] == "/task"
    assert catalog["diagram"]["runtime_types"] == ["codex"]


def test_transport_neutral_manifest_matches_fastmcp_registration() -> None:
    manifest = {
        tool.name: tool
        for tool in platform_invocation.platform_mcp_tool_manifest("workflow")
    }

    assert set(manifest) == {"list_workflows", "get_workflow"}
    assert manifest["list_workflows"].inputSchema["type"] == "object"
    assert manifest["get_workflow"].annotations.readOnlyHint is True
    with pytest.raises(ValueError, match="unknown platform MCP server"):
        platform_invocation.platform_mcp_tool_manifest("missing")


@pytest.mark.asyncio
async def test_transport_neutral_gateway_verifies_and_invokes_without_http_context(
    monkeypatch,
) -> None:
    capability = _capability()
    tool = SimpleNamespace(
        name="get_workflow",
        description="Get one workflow.",
        args={"workflow_id": {"type": "string"}},
    )
    invoke = AsyncMock(return_value=([types.TextContent(type="text", text="ok")], {"status": "success"}))
    verify = Mock(return_value=capability)
    monkeypatch.setitem(
        platform_invocation._PLATFORM_MCP_TOOLSETS,
        "workflow",
        (tool,),
    )
    monkeypatch.setattr(
        platform_invocation,
        "verify_platform_mcp_capability",
        verify,
    )
    monkeypatch.setattr(
        platform_invocation,
        "invoke_platform_mcp_tool_with_capability",
        invoke,
    )

    result = await platform_invocation.invoke_platform_mcp_tool(
        server="workflow",
        tool_name="get_workflow",
        arguments={"workflow_id": "workflow-a"},
        capability_token="turn-capability",
    )

    assert result[1] == {"status": "success"}
    verify.assert_called_once_with(
        "turn-capability",
        secret=platform_invocation.config.signing_secret,
        server="workflow",
    )
    invoke.assert_awaited_once_with(
        tool,
        {"workflow_id": "workflow-a"},
        "workflow",
        capability,
    )


@pytest.mark.asyncio
async def test_transport_neutral_gateway_rejects_bad_capability_and_arguments(
    monkeypatch,
) -> None:
    invoke = AsyncMock()
    monkeypatch.setattr(
        platform_invocation,
        "invoke_platform_mcp_tool_with_capability",
        invoke,
    )
    monkeypatch.setattr(
        platform_invocation,
        "verify_platform_mcp_capability",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(PermissionError, match="invalid or expired"):
        await platform_invocation.invoke_platform_mcp_tool(
            server="workflow",
            tool_name="get_workflow",
            arguments={"workflow_id": "workflow-a"},
            capability_token="invalid",
        )

    monkeypatch.setattr(
        platform_invocation,
        "verify_platform_mcp_capability",
        lambda *_args, **_kwargs: _capability(),
    )
    monkeypatch.setitem(
        platform_invocation._PLATFORM_MCP_TOOLSETS,
        "workflow",
        (
            SimpleNamespace(
                name="get_workflow",
                description="Get one workflow.",
                args={"workflow_id": {"type": "string"}},
            ),
        ),
    )
    result = await platform_invocation.invoke_platform_mcp_tool(
        server="workflow",
        tool_name="get_workflow",
        arguments={},
        capability_token="valid",
    )
    assert isinstance(result, types.CallToolResult)
    assert result.isError is True
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_discovery_is_default_and_build_mutations_are_separate() -> None:
    tools = await _tools("workflow")
    assert set(tools) == {"list_workflows", "get_workflow"}
    assert all(tool.annotations.readOnlyHint for tool in tools.values())

    tools = await _tools("build")
    assert set(tools) == {
        "set_workflow",
        "create_workflow",
        "get_node_spec",
        "check_workflow",
        "update_canvas",
        "new_version",
        "run_workflow",
        "node_execute",
        "batch_execute",
    }
    assert tools["update_canvas"].inputSchema["properties"]["require_valid"]["default"] is True
    # Platform authorization is decided from concrete CallTool arguments by
    # the backend-owned HITL protocol, not by a Runtime-specific MCP hint.
    assert tools["update_canvas"].annotations.destructiveHint is False
    assert tools["run_workflow"].annotations.readOnlyHint is False
    assert tools["batch_execute"].inputSchema["properties"]["row_concurrency"]["default"] == 4


@pytest.mark.asyncio
async def test_task_and_deployment_platform_mcps_export_crud_contracts() -> None:
    task_tools = await _tools("task")
    assert set(task_tools) == {
        "task_list",
        "task_get",
        "task_collect_diagnostics",
        "task_create_scheduled_run",
        "task_update_scheduled_run",
        "task_delete_scheduled_run",
        "task_cancel",
        "task_resume",
    }
    assert task_tools["task_list"].annotations.readOnlyHint is True
    assert task_tools["task_collect_diagnostics"].annotations.readOnlyHint is True
    assert (
        task_tools["task_create_scheduled_run"]
        .inputSchema["properties"]["require_user_auth"]["default"]
        is True
    )
    assert task_tools["task_delete_scheduled_run"].annotations.destructiveHint is True

    deployment_tools = await _tools("deployment")
    assert set(deployment_tools) == {
        "deployment_list",
        "deployment_get",
        "deployment_collect_diagnostics",
        "deployment_create",
        "deployment_update",
        "deployment_delete",
    }
    assert deployment_tools["deployment_get"].annotations.readOnlyHint is True
    assert (
        deployment_tools["deployment_collect_diagnostics"]
        .annotations.readOnlyHint
        is True
    )
    assert (
        deployment_tools["deployment_create"]
        .inputSchema["properties"]["require_user_auth"]["default"]
        is True
    )
    assert deployment_tools["deployment_delete"].annotations.destructiveHint is True


@pytest.mark.asyncio
async def test_knowledge_platform_mcp_exports_package_contract() -> None:
    tools = await _tools("knowledge")
    assert set(tools) == {
        "knowledge_list",
        "knowledge_get",
        "knowledge_create",
        "knowledge_update",
        "knowledge_delete",
        "knowledge_search",
    }
    assert tools["knowledge_list"].annotations.readOnlyHint is True
    assert tools["knowledge_get"].annotations.readOnlyHint is True
    assert tools["knowledge_search"].annotations.readOnlyHint is True
    assert tools["knowledge_create"].annotations.destructiveHint is True
    assert tools["knowledge_update"].annotations.destructiveHint is True
    assert tools["knowledge_delete"].annotations.destructiveHint is True
    assert (
        tools["knowledge_search"]
        .inputSchema["properties"]["top_k"]["default"]
        == 5
    )


def test_sandbox_diagram_catalog_identifies_official_mcp_tools() -> None:
    entry = sandbox_mcp_catalog()[0]
    assert [tool["name"] for tool in entry["tools"]] == list(
        DIAGRAM_MCP_TOOL_NAMES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_name", "server"),
    [
        ("config", "config"),
        ("interactive", "interactive"),
        ("workflow", "workflow"),
        ("task", "task"),
        ("deployment", "deployment"),
        ("knowledge", "knowledge"),
        ("build", "build"),
    ],
)
async def test_every_registered_platform_tool_has_a_signed_action_ceiling(
    server_name: str,
    server,
) -> None:
    policy = platform_mcp_policy(
        organization_id="tenant-a",
        chat_id="chat-a",
        workspace_scope_id="workspace-a",
        server=server_name,
    )
    capability = _capability(
        server=server_name,
        actions=policy.actions,
    )
    for tool_name in await _tools(server):
        platform_invocation._require_tool_capability(
            capability,
            server=server_name,
            tool_name=tool_name,
        )


@pytest.mark.asyncio
async def test_platform_tool_invocation_scopes_and_restores_sync_tenant(monkeypatch) -> None:
    capability = _capability()
    context = SimpleNamespace(pending_vibe=None, workflow={})

    async def fake_context(_capability):
        assert current_sync_tenant_id.get() == "tenant-a"
        return context

    async def coroutine(*, runtime, value):
        assert runtime.context is context
        assert current_sync_tenant_id.get() == "tenant-a"
        return value, {
            "schema_version": 1,
            "status": "success",
            "meta": {"tool": "example"},
        }

    monkeypatch.setattr(platform_invocation, "_context_for", fake_context)
    monkeypatch.setattr(
        platform_invocation,
        "prepare_platform_tool",
        AsyncMock(),
    )
    tool = SimpleNamespace(name="get_workflow", coroutine=coroutine)

    outer = current_sync_tenant_id.set("outer-tenant")
    try:
        content, artifact = (
            await platform_invocation.invoke_platform_mcp_tool_with_capability(
                tool,
                {"value": "ok"},
                "workflow",
                capability,
            )
        )
        assert content[0].text == "ok"
        assert artifact["status"] == "success"
        assert current_sync_tenant_id.get() == "outer-tenant"
    finally:
        current_sync_tenant_id.reset(outer)


@pytest.mark.asyncio
async def test_platform_tool_error_restores_sync_tenant(monkeypatch) -> None:
    capability = _capability(tenant_id="tenant-b", organization_id="tenant-b")
    context = SimpleNamespace(pending_vibe=None, workflow={})

    async def coroutine(*, runtime):
        return "invalid workflow", {
            "schema_version": 1,
            "status": "error",
            "meta": {"tool": "check_workflow"},
        }

    async def fake_context(_capability):
        return context

    monkeypatch.setattr(platform_invocation, "_context_for", fake_context)
    monkeypatch.setattr(
        platform_invocation,
        "prepare_platform_tool",
        AsyncMock(),
    )
    tool = SimpleNamespace(name="get_workflow", coroutine=coroutine)

    outer = current_sync_tenant_id.set("outer-tenant")
    try:
        result = await platform_invocation.invoke_platform_mcp_tool_with_capability(
            tool,
            {},
            "workflow",
            capability,
        )
        assert isinstance(result, types.CallToolResult)
        assert result.isError is True
        payload = json.loads(result.content[0].text)
        assert payload["error"] == {
            "code": "platform_tool_failed",
            "message": "invalid workflow",
        }
        assert current_sync_tenant_id.get() == "outer-tenant"
    finally:
        current_sync_tenant_id.reset(outer)


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

    monkeypatch.setattr(platform_invocation, "session_scope", fake_session_scope)
    monkeypatch.setattr(platform_invocation, "AgentRunsRepo", FakeRunsRepo)
    _allow_context_checks(monkeypatch)

    with pytest.raises(PermissionError, match="active run"):
        await platform_invocation._context_for(capability)


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

    monkeypatch.setattr(platform_invocation, "session_scope", fake_session_scope)
    monkeypatch.setattr(platform_invocation, "AgentRunsRepo", FakeRunsRepo)
    monkeypatch.setattr(platform_invocation, "ChatRepo", FakeChatRepo)
    _allow_context_checks(monkeypatch)
    monkeypatch.setattr(
        platform_invocation,
        "chat_workspace_scope_id",
        lambda _chat_id: "workspace-from-database",
    )

    with pytest.raises(PermissionError, match="workspace"):
        await platform_invocation._context_for(capability)


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

    monkeypatch.setattr(platform_invocation, "session_scope", fake_session_scope)
    monkeypatch.setattr(platform_invocation, "AgentRunsRepo", FakeRunsRepo)
    monkeypatch.setattr(platform_invocation, "ChatRepo", FakeChatRepo)
    _allow_context_checks(monkeypatch)
    monkeypatch.setattr(platform_invocation, "SyncWorkflowRepo", FakeWorkflowRepo)
    monkeypatch.setattr(
        platform_invocation,
        "chat_workspace_scope_id",
        lambda _chat_id: "workspace-a",
    )

    context = await platform_invocation._context_for(capability)

    assert context.workflow == {}
    assert context.current_workflow_id == "workflow-a"
    assert context.authorization_membership_id == "membership-a"
    assert context.authorization_membership_role == "member"
    assert context.authorization_membership_status == "active"
    assert context.authorization_session_id == "session-a"
    assert context.authorization_session_generation == 3
    assert context.runtime_session_id == "runtime-a"
