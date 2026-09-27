from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vibecanvas_api.flowork_cli import cli
from vibecanvas_api.services.agent_runtime import cli_config, cli_host
from vibecanvas_api.services import workflow_model_policy as policy


def row(**changes):
    return {"id": "cred", "user_id": "user", "name": "manual-model", "connection_kind": "manual",
            "enabled": True, "secret_ref": "secret-ref", "provider": "openai",
            "model_name": "private-model", "api_url": "private-url", "api_key": "private-key",
            "description": "Example", "model_context_tokens": 1234, **changes}


@pytest.mark.parametrize("changes", [{"connection_kind": "openrouter_oauth"}, {"connection_kind": None},
    {"enabled": False}, {"deleted_at": "yesterday"}, {"secret_ref": None}, {"model_name": ""}])
def test_ineligible_models_are_excluded(changes):
    assert policy.model_catalog([row(**changes)]) == {}


def test_model_projection_is_manual_provider_independent_and_secretless():
    result = policy.model_catalog([row(provider="openrouter")])
    assert result == {"manual-model": {"provider": "openrouter", "description": "Example", "context_window_tokens": 1234}}
    assert "private" not in str(result)
    assert "source" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,credential_id", [
    ({}, None), ({"connection_kind": "openrouter_oauth"}, "11111111-1111-4111-8111-111111111111"),
    ({"user_id": "other"}, "11111111-1111-4111-8111-111111111111"),
])
async def test_broker_rejects_legacy_workflow_capabilities_before_resolving_secrets(monkeypatch, changes, credential_id):
    from fastapi import HTTPException
    from vibecanvas_api.routes import runtime_model_broker as broker
    from vibecanvas_api.services.agent_runtime.workflow_model_capability import RuntimeWorkflowModelCapability
    from vibecanvas_api.authorization.types import PrincipalRef, PrincipalType

    capability = RuntimeWorkflowModelCapability(
        organization_id="org", user_id="user", workflow_id="wf", execution_id="run",
        execution_resource_type="workflow_execution", credential_id=credential_id,
        provider="openai", model="model", config_revision="rev", authorization_generation="auth",
        issued_at=1, expires_at=9999999999,
    )
    repo = Mock(get_for_user=AsyncMock(return_value=row(**changes)))
    monkeypatch.setattr(broker, "LlmCredentialsRepo", lambda _: repo)
    monkeypatch.setattr(broker, "secret_service", lambda: pytest.fail("must not resolve provider secret"))
    with pytest.raises(HTTPException) as exc:
        await broker._resolve_model_material(session=object(),
            service=Mock(check=AsyncMock(return_value=SimpleNamespace(allowed=True))),
            principal=PrincipalRef(PrincipalType.USER, "user"), authz_context=object(), capability=capability)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "workflow_manual_api_required"


@pytest.mark.asyncio
async def test_models_check_live_use_permission_and_never_swallow_outage(monkeypatch):
    repo = Mock(list_for_user=AsyncMock(return_value=[row(), row(name="account", connection_kind="openrouter_oauth")]))
    monkeypatch.setattr(policy, "LlmCredentialsRepo", lambda _: repo)
    service = Mock(check=AsyncMock(return_value=SimpleNamespace(allowed=False)))
    args = dict(service=service, principal=object(), authz_context=SimpleNamespace(active_organization_id="org"))
    assert await policy.workflow_model_catalog_for_user(object(), "user", **args) == {}
    assert service.check.await_count == 1
    repo.list_for_user.assert_awaited_once_with("user")
    service.check.side_effect = RuntimeError("authorization unavailable")
    with pytest.raises(RuntimeError):
        await policy.workflow_model_catalog_for_user(object(), "user", **args)


@pytest.mark.parametrize("arguments", [{}, {"scope": "global"}, {"scope": "chat"},
    {"scope": "model_api", "workflow_id": "wf"}, {"scope": "workflow", "workflow_id": " "},
    {"scope": "workflow", "user_id": "other"}])
def test_reject_invalid_config_arguments(arguments):
    with pytest.raises(cli.CliUsageError):
        cli.validate_arguments("config.get", arguments)


@pytest.mark.parametrize("scope,extra,result", [
    ("model_api", [], {"models": {}}),
    ("workflow", ["--workflow-id", "wf", "--major", "v2"], {"id": "wf", "version": "v1.sv0", "settings": {}, "defaults": {}}),
])
def test_cli_config_dispatch(monkeypatch, capsys, scope, extra, result):
    request = Mock(return_value=result)
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["config", "get", "--scope", scope, *extra], socket_path="socket") == 0
    args = {"scope": scope, **({"workflow_id": "wf", "major": "v2"} if extra else {})}
    request.assert_called_once_with("socket", args, operation="config.get")
    assert '"models"' in capsys.readouterr().out if scope == "model_api" else True


@pytest.mark.asyncio
async def test_config_host_uses_live_identity_and_dispatches(monkeypatch):
    capability = SimpleNamespace(chat_id="chat", turn_id="turn")
    verifier = Mock(return_value=capability)
    monkeypatch.setattr(cli_host, "verify_agent_capability", verifier)
    ctx = object()
    monkeypatch.setattr(cli_host.agent_context, "resolve_context", AsyncMock(return_value=ctx))
    command = AsyncMock(return_value={"models": {}})
    monkeypatch.setattr(cli_config, "get_config", command)
    assert await cli_host.invoke_workflow_command(operation="config.get", identity_token="host", arguments={"scope": "model_api"}) == {"models": {}}
    assert verifier.call_args.kwargs["server"] == "cli"
    command.assert_awaited_once_with(ctx, {"scope": "model_api"})
    cli_host.agent_context.resolve_context.side_effect = PermissionError("revoked")
    assert (await cli_host.invoke_workflow_command(operation="config.get", identity_token="host", arguments={"scope": "model_api"}))["error"] == "permission_denied"
    assert command.await_count == 1


@pytest.mark.asyncio
async def test_empty_model_catalog_explains_prerequisite_without_fabricating_candidates(monkeypatch):
    models = AsyncMock(return_value={})
    monkeypatch.setattr(cli_config, "models_for_context", models)
    ctx = object()
    result = await cli_config.get_config(ctx, {"scope": "model_api"})
    assert result["models"] == {}
    assert "No eligible Workflow model APIs" in result["message"]
    assert "Settings > API credentials" in result["hint"]
    assert "Chat account connections are not Workflow APIs" in result["hint"]
    models.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_nonempty_catalog_keeps_compact_shape(monkeypatch):
    catalog = {"configured-model": {"provider": "openai", "description": "User API"}}
    monkeypatch.setattr(cli_config, "models_for_context", AsyncMock(return_value=catalog))
    assert await cli_config.get_config(object(), {"scope": "model_api"}) == {"models": catalog}


@pytest.mark.asyncio
async def test_workflow_config_reads_explicit_branch_and_projects_only_public_settings(monkeypatch):
    graph = {"__meta__": {"settings": {"timeouts": {"code": 120}, "code_requirements": "pandas==2.2.0",
        "egress": {"allowed_hosts": ["example.com"]}, "agent_tools": {"secret": "hidden"}, "api_key": "hidden"}}}
    snapshot = AsyncMock(return_value={"id": "wf", "version": "v2.sv3", "workflow": graph})
    monkeypatch.setattr(cli_config, "read_workflow_snapshot", snapshot)
    ctx = object()
    result = await cli_config.get_config(ctx, {"scope": "workflow", "workflow_id": "wf", "major": "v2"})
    snapshot.assert_awaited_once_with(ctx, workflow_id="wf", major="v2")
    assert result["version"] == "v2.sv3"
    assert "hidden" not in str(result)
    assert result["settings"]["timeouts"] == {"code": 120}
    assert result["defaults"]["timeouts"]["http"] == 30


@pytest.mark.parametrize("extra", [
    [], ["--workflow-id", "wf"], ["--major", "v2"],
    ["--workflow-id", "wf", "--major", "v0"],
])
def test_config_workflow_never_falls_back_to_chat_state(monkeypatch, capsys, extra):
    request = Mock()
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["config", "get", "--scope", "workflow", *extra], socket_path="socket") == 2
    request.assert_not_called()
    assert '"error": "invalid_arguments"' in capsys.readouterr().out
