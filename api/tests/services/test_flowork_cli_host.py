from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_runtime import cli_host


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["interactive", "browser"])
async def test_cli_rejects_valid_credentials_for_other_surfaces(monkeypatch, scope):
    from vibecanvas_api.config import config
    from vibecanvas_api.services.agent_resources.capability import mint_agent_capability

    token = mint_agent_capability(
        organization_id="org", user_id="user", chat_id="chat", turn_id="turn",
        workspace_scope_id="workspace", runtime_session_id="runtime",
        session_id="session", session_generation=1, membership_id="membership",
        server=scope, authorization_generation="current", secret=config.signing_secret,
        ttl_s=60,
    )
    context = AsyncMock()
    monkeypatch.setattr(cli_host.agent_context, "resolve_context", context)
    with pytest.raises(PermissionError, match="invalid or expired CLI identity"):
        await cli_host.invoke_workflow_command(
            operation="workflow.list", identity_token=token, arguments={},
        )
    context.assert_not_awaited()


@pytest.mark.asyncio
async def test_operation_dispatch_preserves_atomic_failure_and_rechecks_identity(authorized, monkeypatch):
    reply = {"id": "wf", "version": "v2.sv7", "applied": 0, "error": "node_not_found"}
    operation = AsyncMock(return_value=reply)
    monkeypatch.setattr(cli_host, "operate_workflow", operation)
    args = {"workflow_id": "wf", "major": "v1", "operations": [{"op": "node_remove", "node_id": "old"}]}
    assert await cli_host.invoke_workflow_command(operation="workflow.operation", arguments=args, identity_token="host") == reply
    assert operation.await_args.args[0] is authorized[0].return_value
    operation.side_effect = RuntimeError("Commit lost.")
    assert (await cli_host.invoke_workflow_command(operation="workflow.operation", arguments=args, identity_token="host"))["error"] == "result_unknown"
    authorized[0].side_effect = PermissionError("revoked")
    assert (await cli_host.invoke_workflow_command(operation="workflow.operation", arguments=args, identity_token="host"))["error"] == "permission_denied"
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_get_spec_uses_canonical_registry_without_connection(authorized):
    from vibecanvas_api.agents.prompts.node_definitions import available_node_types, build_node_spec
    from vibecanvas_engine.nodes.base import BaseNode
    candidates = available_node_types()
    assert await cli_host.invoke_workflow_command(operation="workflow.get-spec", identity_token="host",
                                                  arguments={"list_types": True}) == {"types": candidates}
    result = await cli_host.invoke_workflow_command(operation="workflow.get-spec", identity_token="host",
                                                   arguments={"node_types": ["CodeNode", "CodeNode"]})
    assert result == {"node_schema": BaseNode.GENERAL_NODE_SCHEMA, "specs": [build_node_spec("CodeNode")]}
    assert result["node_schema"] is not BaseNode.GENERAL_NODE_SCHEMA
    assert authorized[0].await_count == 2
    authorized[1].assert_not_awaited()


@pytest.mark.asyncio
async def test_human_approval_discovery_schema_and_example(authorized):
    from vibecanvas_api.agents.prompts.node_definitions import core_build_node_types
    from vibecanvas_engine import HumanApprovalNode
    from vibecanvas_engine.node import HumanApprovalNode as CompatibilityNode
    from vibecanvas_engine.nodes import ENGINE_PURE_NODE_TYPES

    listing = await cli_host.invoke_workflow_command(
        operation="workflow.get-spec", identity_token="host", arguments={"list_types": True},
    )
    assert "HumanApprovalNode" in listing["types"]
    assert "HumanApprovalNode" in ENGINE_PURE_NODE_TYPES
    assert "HumanApprovalNode" in core_build_node_types()
    assert CompatibilityNode is HumanApprovalNode
    result = await cli_host.invoke_workflow_command(
        operation="workflow.get-spec", identity_token="host",
        arguments={"node_types": ["HumanApprovalNode"]},
    )
    spec, = result["specs"]
    assert spec["config_schema"] == HumanApprovalNode.CONFIG_SCHEMA
    assert spec["examples"]
    for example in spec["examples"]:
        validation = HumanApprovalNode.check(example["node_dict"])
        assert validation["status"] == "success", validation["error_message"]
        assert set(example["node_dict"]["output_fields"]) == {"approved"}
    authorized[1].assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_dispatch_uses_live_identity(authorized, monkeypatch):
    command = AsyncMock(return_value={"id": "wf", "version": "v2.sv5", "changed": True})
    monkeypatch.setattr(cli_host, "layout_workflow", command)
    args = {"workflow_id": "wf", "major": "v2"}
    result = await cli_host.invoke_workflow_command(operation="workflow.layout", identity_token="host", arguments=args)
    assert result["changed"] is True
    command.assert_awaited_once_with(authorized[0].return_value, **args)
    authorized[0].side_effect = PermissionError("revoked")
    assert (await cli_host.invoke_workflow_command(operation="workflow.layout", identity_token="host", arguments=args))["error"] == "permission_denied"
    assert command.await_count == 1


@pytest.mark.asyncio
async def test_get_spec_unknown_type_rejects_entire_query(authorized, monkeypatch):
    monkeypatch.setattr(cli_host, "build_node_spec", lambda *_: pytest.fail("no partial result"))
    result = await cli_host.invoke_workflow_command(operation="workflow.get-spec", identity_token="host",
                                                   arguments={"node_types": ["CodeNode", "codenode"]})
    assert result["error"] == "unknown_node_type" and "specs" not in result
    assert "CodeNode" in result["types"] and "codenode" in result["message"]






@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", [
    ("workflow.version.list", {"workflow_id": "wf"}), ("workflow.version.create", {"workflow_id": "wf", "major": "v1", "note": "Milestone"}),
])
async def test_version_dispatch_uses_live_identity(authorized, monkeypatch, operation, arguments):
    reply = {"id": "wf", "version": "v2.sv0"}
    command = AsyncMock(return_value=reply)
    monkeypatch.setattr(cli_host, "workflow_version_command", command)
    assert await cli_host.invoke_workflow_command(operation=operation, arguments=arguments, identity_token="host") == reply
    command.assert_awaited_once_with(authorized[0].return_value, operation, arguments)
    authorized[0].side_effect = PermissionError("revoked")
    assert (await cli_host.invoke_workflow_command(operation=operation, arguments=arguments, identity_token="host"))["error"] == "permission_denied"
    assert command.await_count == 1


def test_metadata_result_always_reports_global_head():
    result = cli_host._metadata_result({"wf_id": "wf", "active_major": 3, "active_sub": 5})
    assert result["version"] == "v3.sv5" and "connected" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("from_file", [False, True])
async def test_check_uses_saved_snapshot_or_supplied_graph(authorized, monkeypatch, from_file):
    graph = {"__meta__": {"workflow_id": "file-id"}, "start": {"node_type": "StartNode"}}
    snapshot = AsyncMock(return_value={"id": "bound", "version": "v2.sv4", "workflow": graph})
    validate = AsyncMock(return_value=[])
    monkeypatch.setattr(cli_host, "read_workflow_snapshot", snapshot)
    monkeypatch.setattr(cli_host, "validate_workflow_for_context", validate)
    monkeypatch.setattr(cli_host, "collect_workflow_warnings", lambda _graph: [])
    result = await cli_host.invoke_workflow_command(operation="workflow.check", identity_token="host-only", arguments={"workflow": graph} if from_file else {"workflow_id": "wf", "major": "v2"})
    expected = {"valid": True, "node_count": 1}
    if from_file:
        snapshot.assert_not_awaited()
        assert validate.await_args.args[0] is not graph
    else:
        expected.update(id="bound", version="v2.sv4")
        snapshot.assert_awaited_once_with(authorized[0].return_value, workflow_id="wf", major="v2")
    assert result == expected
    validate.assert_awaited_once_with(graph, authorized[0].return_value)
    assert graph["__meta__"]["workflow_id"] == "file-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_errors", [False, True])
async def test_check_returns_compact_diagnostics(authorized, monkeypatch, has_errors):
    errors = [{"node_id": "start", "message": "Missing child.", "kind": "structural"}] if has_errors else []
    monkeypatch.setattr(cli_host, "validate_workflow_for_context", AsyncMock(return_value=errors))
    monkeypatch.setattr(cli_host, "collect_workflow_warnings", lambda _graph: [{"node_id": "start", "message": "Unused output.", "kind": "unused"}])
    result = await cli_host.invoke_workflow_command(operation="workflow.check", identity_token="host-only", arguments={"workflow": {}})
    assert result["valid"] is not has_errors
    assert result["warnings"] == [{"node_id": "start", "message": "Unused output."}]
    if has_errors:
        assert result["errors"] == [{"node_id": "start", "message": "Missing child."}]
    else:
        assert "errors" not in result


@pytest.mark.asyncio
async def test_check_rechecks_identity_and_does_not_report_service_errors_as_invalid(authorized, monkeypatch):
    validate = AsyncMock(side_effect=ToolError("authorization_unavailable", "Authorization is temporarily unavailable."))
    monkeypatch.setattr(cli_host, "validate_workflow_for_context", validate)
    result = await cli_host.invoke_workflow_command(operation="workflow.check", identity_token="host-only", arguments={"workflow": {}})
    assert result["error"] == "authorization_unavailable"
    assert "valid" not in result and "connected" not in result
    authorized[0].side_effect = PermissionError("revoked")
    denied = await cli_host.invoke_workflow_command(operation="workflow.check", identity_token="host-only", arguments={"workflow": {}})
    assert denied["error"] == "permission_denied"
    assert authorized[0].await_count == 2 and validate.await_count == 1


@pytest.fixture
def authorized(monkeypatch):
    capability = SimpleNamespace(chat_id="chat", turn_id="turn", user_id="user")
    monkeypatch.setattr(cli_host, "verify_agent_capability", lambda *_args, **_kwargs: capability)
    context = AsyncMock(return_value=SimpleNamespace(username="user"))
    rows = AsyncMock(return_value=[])
    monkeypatch.setattr(cli_host.agent_context, "resolve_context", context)
    monkeypatch.setattr(cli_host, "list_authorized_workflows", rows)
    return context, rows


@pytest.mark.asyncio
async def test_list_paginates_authorized_rows_without_chat_selection(authorized):
    context, rows = authorized
    rows.return_value = [
        {"wf_id": "shared", "workflow_name": "Shared", "description": None, "active_major": 1, "active_sub": 2},
        {"wf_id": "owned", "active_major": 2, "active_sub": 0},
    ]
    result = await cli_host.invoke_workflow_list(identity_token="host-only", arguments={"limit": 1, "offset": 5})
    assert result == {"workflows": [{"id": "shared", "name": "Shared", "description": "", "version": "v1.sv2"}], "next_offset": 6}
    rows.assert_awaited_once_with(context.return_value, limit=2, offset=5, include_access=False)


@pytest.mark.asyncio
async def test_permissions_and_results_are_not_cached_between_calls(authorized):
    context, rows = authorized
    assert await cli_host.invoke_workflow_list(identity_token="host-only", arguments={}) == {"workflows": [], "next_offset": None}
    context.side_effect = PermissionError("revoked")
    result = await cli_host.invoke_workflow_list(identity_token="host-only", arguments={})
    assert result["error"] == "permission_denied"
    assert context.await_count == 2
    assert rows.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["workflow.download", "workflow.upload"])
async def test_transfer_dispatches_through_live_host_identity(authorized, monkeypatch, operation):
    result = {"id": "wf", "version": "v1.sv3", "node_count": 0}
    if operation == "workflow.download":
        result["workflow"] = {}
    transfer = AsyncMock(return_value=result)
    monkeypatch.setattr(cli_host, "download_workflow" if operation.endswith("download") else "upload_workflow", transfer)
    arguments = {"workflow_id": "wf", "major": "v1", **({} if operation.endswith("download") else {"workflow": {}, "expected_version": "v1.sv2"})}
    assert await cli_host.invoke_workflow_command(operation=operation, identity_token="host-only", arguments=arguments) == result
    assert transfer.await_args.args[0] is authorized[0].return_value
    authorized[0].side_effect = PermissionError("revoked")
    denied = await cli_host.invoke_workflow_command(operation=operation, identity_token="host-only", arguments=arguments)
    assert denied["error"] == "permission_denied"
    assert transfer.await_count == 1


@pytest.mark.asyncio
async def test_upload_post_dispatch_failure_is_unknown(authorized, monkeypatch):
    monkeypatch.setattr(cli_host, "upload_workflow", AsyncMock(side_effect=RuntimeError("commit response lost")))
    result = await cli_host.invoke_workflow_command(operation="workflow.upload", identity_token="host-only", arguments={"workflow_id": "wf", "major": "v1", "workflow": {}, "expected_version": "v1.sv2"})
    assert result["error"] == "result_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"limit": True}, {"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": "1"}, {"user_id": "other"}])
async def test_host_revalidates_untrusted_arguments(authorized, args):
    context, rows = authorized
    result = await cli_host.invoke_workflow_list(identity_token="host-only", arguments=args)
    assert result["error"] == "invalid_arguments"
    context.assert_not_awaited()
    rows.assert_not_awaited()


@pytest.fixture
def mutations(authorized, monkeypatch):
    meta = {"wf_id": "new", "workflow_name": "Name", "description": "D", "tags": ["T"], "active_major": 1, "active_sub": 0}
    create = AsyncMock(return_value=SimpleNamespace(meta=meta))
    connect = AsyncMock(return_value=meta)
    validate = AsyncMock(return_value=[])
    permission = AsyncMock()
    monkeypatch.setattr(cli_host, "create_authorized_workflow", create)
    monkeypatch.setattr(cli_host, "validate_workflow_for_context", validate)
    monkeypatch.setattr(cli_host, "require_organization_create", permission)
    return create, connect, validate, permission


@pytest.mark.asyncio
async def test_create_validates_import_and_ignores_old_identity(authorized, mutations):
    create, connect, validate, _ = mutations
    original = {"__meta__": {"workflow_id": "old", "workflow_name": "Old", "workflow_version": 9, "settings": {"keep": True}}, "start": {"node_type": "StartNode"}}
    result = await cli_host.invoke_workflow_command(operation="workflow.create", identity_token="host-only", arguments={"name": "Name", "description": "D", "tags": ["T"], "workflow": original})
    assert result == {"id": "new", "name": "Name", "description": "D", "tags": ["T"], "version": "v1.sv0"}
    assert original["__meta__"]["workflow_id"] == "old"
    validated = validate.await_args.args[0]
    assert validated["__meta__"] == {"settings": {"keep": True}}
    create.assert_awaited_once_with(authorized[0].return_value, name="Name", description="D", tags=["T"], initial_workflow=validated)
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_without_file_is_empty_draft(mutations):
    create, _, validate, _ = mutations
    result = await cli_host.invoke_workflow_command(operation="workflow.create", identity_token="host-only", arguments={"name": "Name"})
    assert "connected" not in result
    assert create.await_args.kwargs["initial_workflow"] is None
    assert "bind_chat" not in create.await_args.kwargs
    validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_file_or_denied_create_never_changes_binding(mutations):
    create, connect, validate, permission = mutations
    validate.return_value = [{"node_id": "start", "message": "Missing child"}]
    result = await cli_host.invoke_workflow_command(operation="workflow.create", identity_token="host-only", arguments={"name": "Name", "workflow": {}})
    assert result["error"] == "invalid_workflow"
    create.assert_not_awaited()
    connect.assert_not_awaited()
    permission.side_effect = ToolError("permission_denied", "No create permission")
    result = await cli_host.invoke_workflow_command(operation="workflow.create", identity_token="host-only", arguments={"name": "Name"})
    assert result["error"] == "permission_denied"
    create.assert_not_awaited()




@pytest.mark.asyncio
async def test_created_but_authorization_pending_keeps_id_in_error(mutations):
    create, *_ = mutations
    create.side_effect = ToolError("authorization_pending", "Saved; projection pending", info={"id": "saved", "created": True, "authorization_ready": False})
    result = await cli_host.invoke_workflow_command(operation="workflow.create", identity_token="host-only", arguments={"name": "Name"})
    assert result["error"] == "authorization_pending"
    assert result["id"] == "saved"
    assert result["created"] is True
    assert "Do not create" in result["hint"]


@pytest.mark.asyncio
async def test_authorization_outage_is_not_reported_as_no_workflows(authorized):
    _, rows = authorized
    rows.side_effect = ToolError("authorization_unavailable", "Authorization is temporarily unavailable.")
    result = await cli_host.invoke_workflow_list(identity_token="host-only", arguments={})
    assert result["error"] == "authorization_unavailable"
    assert result["message"] == "Authorization is temporarily unavailable."


@pytest.mark.asyncio
async def test_invalid_identity_never_reaches_resource_query(authorized, monkeypatch):
    context, rows = authorized
    monkeypatch.setattr(cli_host, "verify_agent_capability", lambda *_args, **_kwargs: None)
    with pytest.raises(PermissionError):
        await cli_host.invoke_workflow_list(identity_token="forged", arguments={})
    context.assert_not_awaited()
    rows.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_reads_latest_metadata_and_empty_binding(authorized, monkeypatch):
    meta = {"wf_id": "bound", "workflow_name": "Fresh", "description": "D", "tags": ["t"], "active_major": 2, "active_sub": 8}
    status = AsyncMock(return_value=meta)
    monkeypatch.setattr(cli_host, "get_authorized_workflow_metadata", status)
    result = await cli_host.invoke_workflow_command(operation="workflow.get", identity_token="host-only", arguments={"workflow_id": "wf"})
    assert result == {"id": "bound", "name": "Fresh", "description": "D", "tags": ["t"], "version": "v2.sv8"}
    status.assert_awaited_once_with(authorized[0].return_value, "wf", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,expected", [
    (ToolError("workflow_unavailable", "Inaccessible", info={"id": "saved", "connected": False}), "workflow_unavailable"),
    (ToolError("authorization_unavailable", "Offline"), "authorization_unavailable"),
    (RuntimeError("Database unavailable"), "platform_unavailable"),
])
async def test_status_errors_preserve_state_semantics(authorized, monkeypatch, exception, expected):
    monkeypatch.setattr(cli_host, "get_authorized_workflow_metadata", AsyncMock(side_effect=exception))
    result = await cli_host.invoke_workflow_command(operation="workflow.get", identity_token="host-only", arguments={"workflow_id": "wf"})
    assert result["error"] == expected
    assert "name" not in result and "description" not in result
    assert "connected" not in result


@pytest.mark.asyncio
async def test_status_identity_authorization_outage_is_not_denial(authorized):
    from vibecanvas_api.authorization.openfga_client import OpenFgaUnavailableError

    error = PermissionError("Platform authorization unavailable")
    error.__cause__ = OpenFgaUnavailableError("offline")
    authorized[0].side_effect = error
    result = await cli_host.invoke_workflow_command(operation="workflow.get", identity_token="host-only", arguments={"workflow_id": "wf"})
    assert result["error"] == "authorization_unavailable"
    assert "connected" not in result


@pytest.mark.asyncio
async def test_update_failure_is_unknown_and_update_passes_only_requested_fields(authorized, monkeypatch):
    update = AsyncMock(side_effect=RuntimeError("commit outcome unknown"))
    monkeypatch.setattr(cli_host, "get_authorized_workflow_metadata", update)
    result = await cli_host.invoke_workflow_command(operation="workflow.update", identity_token="host-only", arguments={"workflow_id": "wf", "tags": ["a,b", "a"]})
    update.assert_awaited_once_with(authorized[0].return_value, "wf", {"tags": ["a", "b"]})
    assert result["error"] == "result_unknown"
    assert "workflow get" in result["hint"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", [
    ("workflow.get", {}), ("workflow.update", {}),
    ("workflow.update", {"name": None}), ("workflow.update", {"tags": ["a,,b"]}),
    ("workflow.update", {"name": "N", "workflow_id": "other", "owner": "other"}),
    ("workflow.update", {"workflow": {}}), ("workflow.update", {"description": None}),
])
async def test_metadata_host_rejects_invalid_or_foreign_targets_before_authorization(authorized, operation, arguments):
    result = await cli_host.invoke_workflow_command(operation=operation, identity_token="host-only", arguments=arguments)
    assert result["error"] == "invalid_arguments"
    authorized[0].assert_not_awaited()
