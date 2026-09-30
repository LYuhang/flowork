import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from vibecanvas_api.authorization.types import Action, PrincipalType
from vibecanvas_api.services import deployment_model_dependencies as deps


@pytest.fixture
def setup(monkeypatch):
    ids = {name: str(uuid.uuid4()) for name in (
        "tenant_id", "user_id", "execution_id", "service_account_id", "credential_id",
    )}
    session = Mock(execute=AsyncMock(side_effect=[
        Mock(first=Mock(return_value=(ids["service_account_id"],))),
        Mock(scalar_one_or_none=Mock(return_value=SimpleNamespace(
            membership_id=uuid.uuid4(), org_role="owner", status="active",
        ))),
    ]))
    row = {"id": ids["credential_id"], "name": "manual", "user_id": ids["user_id"],
        "enabled": True, "connection_kind": "manual", "secret_ref": "encrypted",
        "provider": "openai", "model_name": "test"}
    credentials = Mock(list_for_user=AsyncMock(return_value=[row]))
    accounts = Mock(credential_ids=AsyncMock(return_value=()), bind_credential=AsyncMock())
    service = Mock(check=AsyncMock(return_value=SimpleNamespace(allowed=True)))
    enqueue = AsyncMock(return_value=(uuid.uuid4(),))
    monkeypatch.setattr(deps, "LlmCredentialsRepo", lambda _: credentials)
    monkeypatch.setattr(deps, "ServiceAccountsRepo", lambda _: accounts)
    monkeypatch.setattr(deps, "authz_service_for_session", lambda **_: service)
    monkeypatch.setattr(deps, "enqueue_structural_delta", enqueue)
    args = {key: value for key, value in ids.items() if key != "credential_id"}
    args.update(session=session, client=object(), coordinator=object(),
        workflow_id="wf", generation=3, names={"manual"})
    return SimpleNamespace(ids=ids, args=args, session=session, row=row,
        accounts=accounts, credentials=credentials, service=service, enqueue=enqueue)


@pytest.mark.asyncio
async def test_new_version_dependencies_are_limited_and_authorized(setup):
    s = setup
    s.credentials.list_for_user.return_value.append({**s.row, "id": str(uuid.uuid4()), "name": "unused"})
    assert await deps._refresh(**s.args) == s.enqueue.return_value
    s.credentials.list_for_user.assert_awaited_once_with(s.ids["user_id"])
    assert s.accounts.bind_credential.await_count == 1
    assert str(s.accounts.bind_credential.call_args.kwargs["credential_id"]) == s.ids["credential_id"]
    calls = s.service.check.call_args_list
    assert [call.args[1] for call in calls] == [Action.EXECUTE, Action.EXECUTE, Action.USE]
    assert calls[-1].args[0].type is PrincipalType.USER
    assert calls[0].args[0].type is PrincipalType.SERVICE_ACCOUNT
    assert {edge.object_id for edge in s.enqueue.call_args.kwargs["after"]} == {s.ids["credential_id"]}


@pytest.mark.asyncio
async def test_existing_binding_does_not_regrant_revoked_fga_access(setup):
    s = setup
    s.accounts.credential_ids.return_value = (uuid.UUID(s.ids["credential_id"]),)
    assert await deps._refresh(**s.args) == ()
    s.accounts.bind_credential.assert_not_awaited()
    s.enqueue.assert_not_awaited()
    # Existing bindings go on to the broker, which still performs live checks.
    s.service.check.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_check", [0, 1, 2])
async def test_revoked_workflow_execution_or_creator_use_never_grants(setup, failed_check):
    s = setup
    s.service.check.side_effect = [SimpleNamespace(allowed=i != failed_check) for i in range(3)]
    with pytest.raises(PermissionError):
        await deps._refresh(**s.args)
    s.accounts.bind_credential.assert_not_awaited()
    s.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_inactive_or_foreign_execution_never_reads_credentials(setup):
    s = setup
    s.session.execute.side_effect = [Mock(first=Mock(return_value=None))]
    with pytest.raises(PermissionError, match="identity_unavailable"):
        await deps._refresh(**s.args)
    s.credentials.list_for_user.assert_not_awaited()
    s.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_inactive_creator_cannot_expand_delegation(setup):
    s = setup
    s.session.execute.side_effect = [Mock(first=Mock(return_value=(1,))),
        Mock(scalar_one_or_none=Mock(return_value=None))]
    with pytest.raises(PermissionError, match="delegation_denied"):
        await deps._refresh(**s.args)
    s.accounts.bind_credential.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"enabled": False}, {"connection_kind": "openrouter_oauth"},
    {"name": "different"}, {"secret_ref": None}])
async def test_missing_disabled_or_account_models_fail_closed(setup, changes):
    setup.row.update(changes)
    with pytest.raises(PermissionError, match="dependency_unavailable"):
        await deps._refresh(**setup.args)
    setup.accounts.bind_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_model_nodes_do_not_open_authorization_client(monkeypatch):
    monkeypatch.setattr(deps, "openfga_client_from_config", lambda: pytest.fail("no dependencies"))
    await deps.refresh_deployment_model_dependencies(tenant_id="unused", user_id="unused",
        workflow_id="wf", execution_id="unused", service_account_id="unused",
        generation=1, workflow={"start": {"node_type": "StartNode"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("projection_fails", [False, True])
async def test_projection_runs_after_commit_and_failure_propagates(monkeypatch, projection_fails):
    committed = False
    client = Mock(close=AsyncMock())
    mutations = (uuid.uuid4(),)

    @asynccontextmanager
    async def scope(**_):
        nonlocal committed
        yield object()
        committed = True

    async def apply(coordinator, ids):
        assert committed and ids == mutations
        if projection_fails:
            raise RuntimeError("authorization unavailable")

    monkeypatch.setattr(deps, "short_session_scope", scope)
    monkeypatch.setattr(deps, "openfga_client_from_config", lambda: client)
    monkeypatch.setattr(deps, "_refresh", AsyncMock(return_value=mutations))
    monkeypatch.setattr(deps, "apply_committed_structural_mutations", apply)
    args = {"tenant_id": "org", "user_id": "user", "workflow_id": "wf",
        "execution_id": "exec", "service_account_id": "account", "generation": 1,
        "workflow": {"node": {"node_type": "PromptNode", "node_config": {"model_name": "manual"}}}}
    if projection_fails:
        with pytest.raises(RuntimeError, match="authorization unavailable"):
            await deps.refresh_deployment_model_dependencies(**args)
    else:
        await deps.refresh_deployment_model_dependencies(**args)
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_execution_identity_query_and_membership_with_real_database(client, monkeypatch):
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_deployment_invocations import (
        DeploymentInvocationsRepo,
    )
    from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
    from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo

    response = await client.post("/api/v1/auth/register", json={
        "email": f"dependency-{uuid.uuid4().hex}@example.com", "username": "dep-test",
        "password": "pw12345678",
    })
    assert response.status_code == 201, response.text
    registered = response.json()
    tenant = registered["session"]["active_organization_id"]
    user = registered["user"]["user_id"]
    account, deployment, credential = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workflow_id = f"dep-{uuid.uuid4().hex}"
    async with session_scope(tenant) as session:
        await WorkflowRepo(session, user).create_workflow(wf_id=workflow_id, name="Dependency test")
        await ServiceAccountsRepo(session).create_for_owner(service_account_id=account,
            tenant_id=uuid.UUID(tenant), name="Deployment", kind="deployment",
            owner_resource_type="deployment", owner_resource_id=str(deployment), created_by=uuid.UUID(user))
        await DeploymentsRepo(session).insert(id=deployment, tenant_id=uuid.UUID(tenant),
            user_id=uuid.UUID(user), owner_id=uuid.UUID(user), service_account_id=account,
            wf_id=workflow_id, name="D", slug=f"dep-{deployment.hex}", trigger_type="api",
            version_pin="major", pinned_major=1, api_key_hash="test-only-hash")
        invocation = await DeploymentInvocationsRepo(session).create(tenant_id=uuid.UUID(tenant),
            deployment_id=deployment, wf_id=workflow_id, trigger_type="api", source="sync_api", status="running")

    monkeypatch.setattr(deps, "LlmCredentialsRepo", lambda _: Mock(list_for_user=AsyncMock(return_value=[{
        "id": credential, "name": "manual", "user_id": user, "enabled": True,
        "connection_kind": "manual", "secret_ref": "encrypted", "provider": "openai", "model_name": "test",
    }])))
    bind = AsyncMock()
    monkeypatch.setattr(ServiceAccountsRepo, "bind_credential", bind)
    enqueue = AsyncMock(return_value=(uuid.uuid4(),))
    monkeypatch.setattr(deps, "enqueue_structural_delta", enqueue)
    monkeypatch.setattr(deps, "authz_service_for_session", lambda **_: Mock(
        check=AsyncMock(return_value=SimpleNamespace(allowed=True))))
    args = {"client": object(), "coordinator": object(), "tenant_id": tenant, "user_id": user,
        "workflow_id": workflow_id, "execution_id": str(invocation), "service_account_id": str(account),
        "generation": 1, "names": {"manual"}}
    async with session_scope(tenant) as session:
        assert await deps._refresh(session=session, **args) == enqueue.return_value
    bind.assert_awaited_once()
    for overrides in ({"generation": 99}, {"user_id": str(uuid.uuid4())},
                      {"workflow_id": "wrong-workflow"}, {"execution_id": str(uuid.uuid4())}):
        async with session_scope(tenant) as session:
            with pytest.raises(PermissionError, match="identity_unavailable"):
                await deps._refresh(session=session, **{**args, **overrides})
    async with session_scope(tenant) as session:
        await ServiceAccountsRepo(session).set_status(account, status="disabled")
    async with session_scope(tenant) as session:
        with pytest.raises(PermissionError, match="identity_unavailable"):
            await deps._refresh(session=session, **args)


@pytest.mark.asyncio
async def test_resource_only_change_grants_selected_dependencies_after_execution_check(setup, monkeypatch):
    s = setup
    from vibecanvas_api.services import service_account_resources
    identifier = str(uuid.uuid4())
    s.args['names'] = set()
    s.args['workflow'] = {'worker': {'node_type': 'SubAgentNode', 'node_config': {
        'mcp_servers': [{'id': identifier, 'name': 'Selected MCP'}]}}}
    s.accounts.resource_refs = AsyncMock(return_value=())
    bind = AsyncMock(return_value=(('mcp_installation', identifier),))
    monkeypatch.setattr(service_account_resources, 'bind_workflow_resources', bind)
    assert await deps._refresh(**s.args) == s.enqueue.return_value
    assert [call.args[1] for call in s.service.check.call_args_list] == [Action.EXECUTE, Action.EXECUTE]
    assert {edge.object_id for edge in s.enqueue.call_args.kwargs['after']} == {identifier}
    s.accounts.resource_refs.return_value = (('mcp_installation', identifier),)
    bind.reset_mock()
    s.enqueue.reset_mock()
    assert await deps._refresh(**s.args) == ()
    bind.assert_not_awaited()
    s.enqueue.assert_not_awaited()
