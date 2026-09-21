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
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client, *, prefix: str) -> tuple[str, dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{prefix}_{uuid.uuid4().hex[:12]}@example.com",
            "username": prefix,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session_token"], response.json()


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

    monkeypatch.setattr(config.agent, "model", "openai:gpt-service-account-test")
    monkeypatch.setattr(config.agent, "api_key", "host-only-test-key")
    monkeypatch.setattr(config.agent, "base_url", "https://provider.example/v1")
    token = mint_runtime_workflow_model_capability(
        organization_id=str(tenant_id),
        user_id=str(user_id),
        workflow_id=workflow_id,
        execution_id=str(task_id),
        execution_resource_type="task",
        credential_id=None,
        provider="openai",
        model="gpt-service-account-test",
        config_revision=model_config_revision(
            provider="openai",
            model="gpt-service-account-test",
            updated_at="platform-process-config",
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
    lease = await asyncio.to_thread(
        _scheduled_execution_lease,
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
        await asyncio.to_thread(
            _scheduled_execution_lease,
            task_id=task_id,
            schedule_id=schedule_id,
            workflow_id=workflow_id,
        )
