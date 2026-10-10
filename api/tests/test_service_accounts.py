from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from starlette.requests import Request

from vibecanvas_api.background_tasks.batch_exec import _task_execution_lease
from vibecanvas_api.background_tasks.scheduled_runs import (
    _scheduled_execution_lease,
)
from vibecanvas_api.config import config
from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.routes.runtime_model_broker import (
    _authorize_and_resolve_workflow_target,
)
from vibecanvas_api.services.agent_runtime.model_capability import (
    authorization_model_generation,
    model_config_revision,
)
from vibecanvas_api.services.agent_runtime.workflow_model_capability import (
    mint_runtime_workflow_model_capability,
    verify_runtime_workflow_model_capability,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


from tests.test_workflow_authorization_integration import _browser_sessions, _headers, _session_headers


async def _register(client, *, prefix: str) -> tuple[dict[str, str], dict]:
    response = await client.post(
        "/api/v1/auth/register",
        headers={"Origin": config.public_urls.public_url or "http://testserver"},
        json={"email": f"{prefix}_{uuid.uuid4().hex[:12]}@example.com",
              "username": prefix, "password": "pw12345678"},
    )
    assert response.status_code == 201, response.text
    return _session_headers(response, client), response.json()


async def _seed_identity(app_engine, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with app_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:tenant_id, 'org')"),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:user_id, :tenant_id, :email)"
            ),
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "email": f"service-account-{user_id}@example.test",
            },
        )


@pytest.mark.asyncio
async def test_service_account_disable_increments_generation_and_revokes_lease(
    app_engine,
):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    await _seed_identity(app_engine, tenant_id=tenant_id, user_id=user_id)

    async with session_scope(str(tenant_id)) as session:
        repo = ServiceAccountsRepo(session)
        await repo.create_for_owner(
            service_account_id=account_id,
            tenant_id=tenant_id,
            name="Batch task identity",
            kind="task",
            owner_resource_type="task",
            owner_resource_id=str(uuid.uuid4()),
            created_by=user_id,
        )
        lease = await repo.require_active_lease(
            service_account_id=account_id,
            owner_resource_type="task",
            owner_resource_id=(
                await repo.get(account_id)
            ).owner_resource_id,
        )
        assert lease.generation == 1
        row = await repo.set_status(account_id, status="disabled")
        assert row.generation == 2

    async with session_scope(str(tenant_id)) as session:
        repo = ServiceAccountsRepo(session)
        with pytest.raises(LookupError, match="service_account_unavailable"):
            await repo.require_active_lease(
                service_account_id=account_id,
                owner_resource_type="task",
                owner_resource_id=lease.owner_resource_id,
                generation=lease.generation,
            )
        row = await repo.set_status(account_id, status="active")
        assert row.generation == 3


@pytest.mark.asyncio
async def test_organization_service_account_review_disable_and_rotate(
    client,
):
    token, registered = await _register(client, prefix="service-account-admin")
    tenant_id = uuid.UUID(registered["session"]["active_organization_id"])
    user_id = uuid.UUID(registered["user"]["user_id"])
    account_id = uuid.uuid4()
    async with session_scope(str(tenant_id)) as session:
        await ServiceAccountsRepo(session).create_for_owner(
            service_account_id=account_id,
            tenant_id=tenant_id,
            name="Deployment automation identity",
            kind="deployment",
            owner_resource_type="deployment",
            owner_resource_id=str(uuid.uuid4()),
            created_by=user_id,
        )

    listed = await client.get(
        f"/api/v1/organizations/{tenant_id}/service-accounts",
        headers=_headers(token),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == [
        {
            **listed.json()["items"][0],
            "service_account_id": str(account_id),
            "name": "Deployment automation identity",
            "status": "active",
            "generation": 1,
            "credential_ids": [],
        }
    ]

    disabled = await client.patch(
        f"/api/v1/organizations/{tenant_id}/service-accounts/{account_id}",
        headers=_headers(token),
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["generation"] == 2

    rotated = await client.post(
        f"/api/v1/organizations/{tenant_id}/service-accounts/{account_id}/rotate",
        headers=_headers(token),
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["status"] == "disabled"
    assert rotated.json()["generation"] == 3

    foreign_token, foreign = await _register(
        client,
        prefix="service-account-foreign",
    )
    foreign_list = await client.get(
        f"/api/v1/organizations/{tenant_id}/service-accounts",
        headers=_headers(foreign_token),
    )
    assert foreign_list.status_code == 404
    assert foreign["session"]["active_organization_id"] != str(tenant_id)


@pytest.mark.asyncio
async def test_service_accounts_are_force_rls_isolated(app_engine):
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    user_a = uuid.uuid4()
    await _seed_identity(app_engine, tenant_id=tenant_a, user_id=user_a)
    async with app_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:tenant_id, 'org-b')"),
            {"tenant_id": tenant_b},
        )

    account_id = uuid.uuid4()
    async with session_scope(str(tenant_a)) as session:
        await ServiceAccountsRepo(session).create_for_owner(
            service_account_id=account_id,
            tenant_id=tenant_a,
            name="Deployment identity",
            kind="deployment",
            owner_resource_type="deployment",
            owner_resource_id=str(uuid.uuid4()),
            created_by=user_a,
        )

    async with app_engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": str(tenant_b)},
        )
        rows = (
            await connection.execute(
                text("SELECT service_account_id FROM service_accounts")
            )
        ).all()
    assert rows == []


async def _seed_running_task_with_account(
    app_engine,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    account_id = uuid.uuid4()
    workflow_id = f"wf-service-account-{uuid.uuid4().hex}"
    await _seed_identity(app_engine, tenant_id=tenant_id, user_id=user_id)
    async with session_scope(str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=workflow_id,
            name="test",
        )
        await ServiceAccountsRepo(session).create_for_owner(
            service_account_id=account_id,
            tenant_id=tenant_id,
            name="Running task identity",
            kind="task",
            owner_resource_type="task",
            owner_resource_id=str(task_id),
            created_by=user_id,
        )
        await TasksRepo(session).create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=workflow_id,
            task_type="batch_exec",
            payload={},
            service_account_id=account_id,
        )
        await TasksRepo(session).update_status(task_id, status="running")
    return tenant_id, user_id, task_id, workflow_id


@pytest.mark.asyncio
async def test_worker_pickup_uses_database_account_not_queue_user(app_engine):
    tenant_id, user_id, task_id, workflow_id = (
        await _seed_running_task_with_account(app_engine)
    )
    current_sync_tenant_id.set(str(tenant_id))
    lease = await asyncio.to_thread(
        _task_execution_lease,
        task_id,
        workflow_id=workflow_id,
    )
    assert lease.created_by == user_id

    async with session_scope(str(tenant_id)) as session:
        await ServiceAccountsRepo(session).set_status(
            lease.service_account_id,
            status="disabled",
        )
    current_sync_tenant_id.set(str(tenant_id))
    with pytest.raises(LookupError, match="service_account_unavailable"):
        await asyncio.to_thread(
            _task_execution_lease,
            task_id,
            workflow_id=workflow_id,
        )


@pytest.mark.asyncio
async def test_model_broker_revalidates_service_account_generation(
    app_engine,
    monkeypatch,
    openfga_allow_all,
):
    tenant_id, user_id, task_id, workflow_id = (
        await _seed_running_task_with_account(app_engine)
    )
    async with session_scope(str(tenant_id)) as session:
        task = await TasksRepo(session).get(task_id)
        account = await ServiceAccountsRepo(session).get(
            task.service_account_id
        )
        generation = account.generation
        account_id = account.service_account_id

    # Workflow leases require a user-owned manual API, not the Chat default.
    credential_id = uuid.uuid4()
    async with session_scope(str(tenant_id)) as session:
        secret_ref = await secret_service().put_text(
            session,
            tenant_id=tenant_id,
            purpose="llm_api_key",
            resource_type="llm_credential",
            resource_id=credential_id,
            plaintext="service-account-test-key",
        )
        credentials = LlmCredentialsRepo(session)
        await credentials.insert(
            id=credential_id,
            tenant_id=tenant_id,
            user_id=user_id,
            name="service-account-test",
            provider="openai",
            connection_kind="manual",
            model_name="gpt-service-account-test",
            api_url="https://provider.example/v1",
            secret_ref=secret_ref,
            enabled=True,
        )
        credential = await credentials.get(credential_id)
    monkeypatch.setattr(config, "runtime_model_egress_policy", "host")
    token = mint_runtime_workflow_model_capability(
        organization_id=str(tenant_id),
        user_id=str(user_id),
        workflow_id=workflow_id,
        execution_id=str(task_id),
        execution_resource_type="task",
        credential_id=str(credential_id),
        provider="openai",
        model="gpt-service-account-test",
        config_revision=model_config_revision(
            provider="openai",
            model="gpt-service-account-test",
            updated_at=credential["updated_at"],
        ),
        authorization_generation=authorization_model_generation(
            model_id=config.openfga_authorization_model_id,
        ),
        secret=config.signing_secret,
        ttl_s=120,
        principal_type="service_account",
        principal_id=str(account_id),
        principal_generation=generation,
    )
    capability = verify_runtime_workflow_model_capability(
        token,
        secret=config.signing_secret,
    )
    assert capability is not None
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/internal/runtime-model/v1/chat/completions",
        "headers": [],
        "query_string": b"",
        "app": SimpleNamespace(
            state=SimpleNamespace(openfga_client=openfga_allow_all),
        ),
    })
    target = await _authorize_and_resolve_workflow_target(
        request,
        capability,
    )
    assert target.model == "gpt-service-account-test"
    assert target.api_key == "service-account-test-key"

    async with session_scope(str(tenant_id)) as session:
        await ServiceAccountsRepo(session).set_status(
            account_id,
            status="disabled",
        )
    with pytest.raises(HTTPException) as exc_info:
        await _authorize_and_resolve_workflow_target(request, capability)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {
        "code": "runtime_model_service_account_revoked"
    }


@pytest.mark.asyncio
async def test_scheduled_worker_requires_matching_active_account(app_engine):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    schedule_id = uuid.uuid4()
    account_id = uuid.uuid4()
    workflow_id = f"wf-schedule-{uuid.uuid4().hex}"
    await _seed_identity(app_engine, tenant_id=tenant_id, user_id=user_id)
    async with session_scope(str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=workflow_id,
            name="test",
        )
        await ServiceAccountsRepo(session).create_for_owner(
            service_account_id=account_id,
            tenant_id=tenant_id,
            name="Schedule identity",
            kind="schedule",
            owner_resource_type="task",
            owner_resource_id=str(task_id),
            created_by=user_id,
        )
        await TasksRepo(session).create_schedule(
            task_id=task_id,
            schedule_id=schedule_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=workflow_id,
            name="Scheduled test",
            enabled=True,
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=60,
            timezone="UTC",
            input_preset={},
            mount_enabled=False,
            notification_policy={},
            next_run_at=None,
            service_account_id=account_id,
        )

    current_sync_tenant_id.set(str(tenant_id))
    lease = await _scheduled_execution_lease(
        task_id=task_id,
        schedule_id=schedule_id,
        workflow_id=workflow_id,
    )
    assert lease.service_account_id == account_id
    async with session_scope(str(tenant_id)) as session:
        await ServiceAccountsRepo(session).set_status(
            account_id,
            status="disabled",
        )
    current_sync_tenant_id.set(str(tenant_id))
    with pytest.raises(LookupError, match="service_account_unavailable"):
        await _scheduled_execution_lease(
            task_id=task_id,
            schedule_id=schedule_id,
            workflow_id=workflow_id,
        )


@pytest.mark.asyncio
async def test_resource_delegation_revocation_survives_dependency_refresh(app_engine):
    tenant_id, user_id, account_id, skill_id = (uuid.uuid4() for _ in range(4))
    await _seed_identity(app_engine, tenant_id=tenant_id, user_id=user_id)
    async with session_scope(str(tenant_id)) as session:
        repo = ServiceAccountsRepo(session)
        await repo.create_for_owner(service_account_id=account_id, tenant_id=tenant_id,
            name="Resource consumer", kind="task", owner_resource_type="task",
            owner_resource_id=str(uuid.uuid4()), created_by=user_id)
        grant = dict(tenant_id=tenant_id, service_account_id=account_id,
                     resource_type="skill_installation", resource_id=skill_id, resource_tenant_id=tenant_id)
        await repo.bind_resource(**grant)
        assert await repo.resource_refs(account_id) == (("skill_installation", str(skill_id)),)
        await repo.revoke_resource(service_account_id=account_id,
            resource_type="skill_installation", resource_id=skill_id)
        await repo.bind_resource(**grant)
        assert await repo.resource_refs(account_id) == ()
        assert await repo.resource_refs(account_id, include_revoked=True) == (("skill_installation", str(skill_id)),)


@pytest.mark.asyncio
async def test_dependency_ownership_is_separate_and_recipient_scoped(app_engine):
    tenant, owner, user, owner_user, account, skill = (uuid.uuid4() for _ in range(6))
    await _seed_identity(app_engine, tenant_id=tenant, user_id=user)
    await _seed_identity(app_engine, tenant_id=owner, user_id=owner_user)
    async with session_scope(str(tenant)) as session:
        repo = ServiceAccountsRepo(session)
        await repo.create_for_owner(service_account_id=account, tenant_id=tenant,
            name="Shared dependency", kind="task", owner_resource_type="task",
            owner_resource_id=str(uuid.uuid4()), created_by=user)
        grant = dict(tenant_id=tenant, service_account_id=account,
            resource_type="skill_installation", resource_id=skill, resource_tenant_id=owner)
        await repo.bind_resource(**grant)
        assert await repo.resource_owners(account) == {("skill_installation", str(skill)): str(owner)}
        await repo.revoke_resource(service_account_id=account, resource_type="skill_installation", resource_id=skill)
        await repo.bind_resource(**grant)
        assert await repo.resource_owners(account) == {}
        assert await repo.resource_owners(account, include_revoked=True) == {("skill_installation", str(skill)): str(owner)}
    async with session_scope(str(owner)) as session:
        assert await ServiceAccountsRepo(session).resource_owners(account, include_revoked=True) == {}


def test_service_account_projection_uses_dependency_organization():
    from vibecanvas_api.authorization.projection import service_account_edges
    from vibecanvas_api.authorization.mutations import MutationEdge
    account_org, resource_org, account, creator, task, workflow, skill = (str(uuid.uuid4()) for _ in range(7))
    edges = service_account_edges(organization_id=account_org, service_account_id=account,
        created_by=creator, owner_resource_type="task", owner_resource_id=task,
        workflow_id=workflow, resource_owners={("skill_installation", skill): resource_org})
    assert MutationEdge(resource_org, "skill_installation", skill, "consumer", "service_account", account) in edges
    assert MutationEdge(account_org, "skill_installation", skill, "consumer", "service_account", account) not in edges
    assert all(edge.organization_id == account_org for edge in edges if edge.object_type != "skill_installation")


@pytest.mark.asyncio
async def test_shared_dependency_projection_is_collected_only_by_resource_owner(app_engine):
    from vibecanvas_api.authorization.projection import collect_structural_projection
    from vibecanvas_api.authorization.mutations import MutationEdge
    from vibecanvas_api.storage.models_skills import Skill
    tenant, user, task, _ = await _seed_running_task_with_account(app_engine)
    owner, owner_user, skill = (uuid.uuid4() for _ in range(3))
    await _seed_identity(app_engine, tenant_id=owner, user_id=owner_user)
    async with session_scope(str(owner)) as session:
        session.add(Skill(skill_id=skill, tenant_id=owner, user_id=owner_user,
                          name="Shared projection fixture", description="", source="custom"))
    async with session_scope(str(tenant)) as session:
        account = (await session.execute(text("SELECT service_account_id FROM tasks WHERE id=:id"), {"id": task})).scalar_one()
        await ServiceAccountsRepo(session).bind_resource(tenant_id=tenant, service_account_id=account,
            resource_type="skill_installation", resource_id=skill, resource_tenant_id=owner)
    edge = MutationEdge(str(owner), "skill_installation", str(skill), "consumer", "service_account", str(account))
    async with session_scope(str(tenant)) as session:
        assert edge not in await collect_structural_projection(session, organization_id=str(tenant))
    async with session_scope(str(owner)) as session:
        assert edge in await collect_structural_projection(session, organization_id=str(owner))
    async with session_scope(str(tenant)) as session:
        await ServiceAccountsRepo(session).revoke_resource(service_account_id=account,
            resource_type="skill_installation", resource_id=skill)
    async with session_scope(str(owner)) as session:
        assert edge not in await collect_structural_projection(session, organization_id=str(owner))
