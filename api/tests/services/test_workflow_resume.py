"""Original identity fences use real database membership and execution state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from tests.test_scheduled_runs import _seed_tenant_user_workflow
from vibecanvas_api.services import workflow_execution_authorization as authz
from vibecanvas_api.services import workflow_resume as resume
from vibecanvas_api.services.llm_credentials_inject import inject_into_run_context_async
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.execution_repo import ExecutionRepo
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [None, "membership", "permission", "stale_model", "inactive", "wrong_actor", "missing"]
)
async def test_resume_rechecks_original_identity(pg_engine, monkeypatch, change):
    from tests.storage.test_workflow_history import approval_graph

    tenant, actor, wf_id = await _seed_tenant_user_workflow(pg_engine)
    tenant, actor = str(tenant), str(actor)
    execution = str(uuid4())
    workflow = approval_graph()
    async with session_scope(tenant_id=tenant) as session:
        await ExecutionRepo(session, actor).start_execution(wf_id, (1, 0), execution)
        await WorkflowHistoryRepo(session).create(
            execution_id=execution,
            tenant_id=tenant,
            wf_id=wf_id,
            source_type="workflow",
            source_id=wf_id,
            initiator_user_id=actor,
            workflow=workflow,
            inputs={},
            approvers={"node_2": actor},
        )
    context = await inject_into_run_context_async(
        {},
        workflow,
        tenant,
        user_id=actor,
        workflow_id=wf_id,
        execution_id=execution,
        execution_resource_type="workflow_execution",
    )
    service = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=change != "permission")))
    client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(resume, "openfga_client_from_config", lambda: client)
    monkeypatch.setattr(authz, "authz_service_for_session", lambda **kwargs: service)
    if change == "stale_model":
        context[resume.IDENTITY_KEY]["authorization_generation"] = "outdated"
    elif change == "wrong_actor":
        context[resume.IDENTITY_KEY]["user_id"] = str(uuid4())
    elif change == "missing":
        context.clear()
    elif change == "membership":
        async with pg_engine.begin() as connection:
            await connection.execute(
                text("UPDATE org_memberships SET status='suspended' WHERE user_id=:id"), {"id": UUID(actor)}
            )
    elif change == "inactive":
        async with session_scope(tenant_id=tenant) as session:
            await ExecutionRepo(session, actor).stop_execution(execution)
    if change:
        with pytest.raises((HTTPException, PermissionError)):
            await resume.refresh_execution_context(
                tenant_id=tenant, execution_id=execution, workflow=workflow, context=context
            )
    else:
        refreshed = await resume.refresh_execution_context(
            tenant_id=tenant, execution_id=execution, workflow=workflow, context=context
        )
        assert refreshed == {"llm_credentials": {}, "workflow_resources": {}}
        assert service.check.await_count == 2
        for call in service.check.await_args_list:
            assert call.args[0].id == actor
    if change not in {"missing", "wrong_actor"}:
        client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("rotated", [False, True])
async def test_resume_keeps_original_service_account_generation(pg_engine, app_engine, monkeypatch, rotated):
    from tests.storage.test_workflow_history import approval_graph
    from tests.test_deployment_invoke_sync import _seed_full_deployment
    from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
    from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo

    tenant, _, _, deployment = await _seed_full_deployment(pg_engine, app_engine)
    account_id, invocation = uuid4(), uuid4()
    async with session_scope(tenant_id=str(tenant)) as session:
        dep = (
            (await session.execute(text("SELECT * FROM deployments WHERE id=:id"), {"id": deployment})).mappings().one()
        )
        account = await ServiceAccountsRepo(session).create_for_owner(
            service_account_id=account_id,
            tenant_id=tenant,
            name="approval executor",
            kind="deployment",
            owner_resource_type="deployment",
            owner_resource_id=str(deployment),
            created_by=dep["user_id"],
        )
        generation = account.generation
        await session.execute(
            text("UPDATE deployments SET service_account_id=:account WHERE id=:id"),
            {"account": account_id, "id": deployment},
        )
        await DeploymentInvocationsRepo(session).create(
            invocation_id=invocation,
            tenant_id=tenant,
            deployment_id=deployment,
            wf_id=dep["wf_id"],
            trigger_type="api",
            source="sync_api",
            status="running",
        )
        await WorkflowHistoryRepo(session).create(
            execution_id=str(invocation),
            tenant_id=str(tenant),
            wf_id=dep["wf_id"],
            source_type="deployment",
            source_id=str(deployment),
            initiator_user_id=str(dep["user_id"]),
            workflow=approval_graph(),
            inputs={},
            approvers={"node_2": str(dep["user_id"])},
        )
    identity = resume.execution_identity(
        tenant_id=str(tenant),
        user_id=str(dep["user_id"]),
        workflow_id=dep["wf_id"],
        execution_id=str(invocation),
        execution_resource_type="deployment_invocation",
        principal_type="service_account",
        principal_id=str(account_id),
        principal_generation=generation,
    )
    if rotated:
        async with session_scope(tenant_id=str(tenant)) as session:
            await session.execute(
                text("UPDATE service_accounts SET generation=generation+1 WHERE service_account_id=:id"),
                {"id": account_id},
            )
    service = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(authz, "authz_service_for_session", lambda **kwargs: service)
    monkeypatch.setattr(resume, "openfga_client_from_config", lambda: SimpleNamespace(close=AsyncMock()))
    args = dict(
        tenant_id=str(tenant),
        execution_id=str(invocation),
        workflow=approval_graph(),
        context={resume.IDENTITY_KEY: identity},
    )
    if rotated:
        with pytest.raises(HTTPException) as denied:
            await resume.refresh_execution_context(**args)
        assert denied.value.detail["code"] == "runtime_model_service_account_revoked"
        service.check.assert_not_awaited()
    else:
        assert await resume.refresh_execution_context(**args) == {"llm_credentials": {}, "workflow_resources": {}}
        assert all(call.args[0].id == str(account_id) for call in service.check.await_args_list)
