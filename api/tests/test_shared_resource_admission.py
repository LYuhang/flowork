"""Recipient projection admission must narrow RLS to one shared root."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from starlette.requests import Request

from vibecanvas_api.auth.deps import AuthContext, _admit_shared_resource
from vibecanvas_api.storage.db import session_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_type", ["workflow", "task", "deployment"])
@pytest.mark.parametrize("entry", ["resource", "history", "execution", "events", "cli"])
async def test_projection_rebinds_rls_and_records_exact_admitted_root(pg_engine, entry, resource_type):
    owner_id = uuid.uuid4()
    recipient_tenant_id = uuid.uuid4()
    recipient_user_id = uuid.uuid4()
    mutation_id = uuid.uuid4()
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants(tenant_id, name) VALUES "
                "(:owner_id, 'Owner'), (:recipient_id, 'Recipient')"
            ),
            {
                "owner_id": owner_id,
                "recipient_id": recipient_tenant_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO users(
                    user_id, tenant_id, email, display_name, status
                ) VALUES (
                    :user_id, :tenant_id, :email, '', 'active'
                )
                """
            ),
            {
                "user_id": recipient_user_id,
                "tenant_id": recipient_tenant_id,
                "email": f"redacted-{recipient_user_id}@invalid.local",
            },
        )
        await connection.execute(text("""
            INSERT INTO organizations (tenant_id, kind, slug, name)
            SELECT tenant_id, 'personal', tenant_id::text, name FROM tenants
            WHERE tenant_id IN (:owner, :recipient)
        """), {'owner': owner_id, 'recipient': recipient_tenant_id})
        await connection.execute(text("""
            INSERT INTO org_memberships (membership_id, tenant_id, user_id, org_role, status)
            VALUES (:id, :tenant, :user, 'owner', 'active')
        """), {'id': uuid.uuid4(), 'tenant': recipient_tenant_id, 'user': recipient_user_id})
        await connection.execute(
            text(
                """
                INSERT INTO authz_mutations(
                    mutation_id, tenant_id, actor_type, actor_id, kind,
                    operation, desired_state, object_type, object_id,
                    relation, subject_type, subject_id, edge_revision,
                    status, revocation_guard_active, idempotency_key,
                    attempt_count, requested_at, applied_at
                ) VALUES (
                    :mutation_id, :owner_id, 'system', 'test',
                    'direct_binding', 'write', 'present', :resource_type,
                    'wf-shared', 'viewer', 'user', :user_id, 1,
                    'applied', false, :idempotency_key, 0, now(), now()
                )
                """
            ),
            {
                "mutation_id": mutation_id,
                "resource_type": resource_type,
                "owner_id": owner_id,
                "user_id": str(recipient_user_id),
                "idempotency_key": f"test-{mutation_id}",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO shared_resource_projections(
                    owner_tenant_id, resource_type, resource_id,
                    recipient_user_id, relation, source_mutation_id,
                    edge_revision
                ) VALUES (
                    :owner_id, :resource_type, 'wf-shared', :user_id,
                    'viewer', :mutation_id, 1
                )
                """
            ),
            {
                "owner_id": owner_id,
                "user_id": recipient_user_id,
                "mutation_id": mutation_id,
                "resource_type": resource_type,
            },
        )

    execution_id = str(uuid.uuid4())
    if entry in {"execution", "events"}:
        from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
        async with session_scope(tenant_id=str(owner_id)) as session:
            await WorkflowHistoryRepo(session).create(
                execution_id=execution_id, tenant_id=str(owner_id), wf_id="wf-shared",
                source_type=resource_type, source_id="wf-shared",
                initiator_user_id=str(recipient_user_id), workflow={}, inputs={},
                approvers={}, workflow_version="v1.sv0",
            )
    path = "/api/v1/workflows/wf-shared"
    params = {{"workflow": "wf_id", "task": "task_id", "deployment": "dep_id"}[resource_type]: "wf-shared"}
    query = b""
    if entry == "history":
        path, params = "/api/v1/workflow-executions", {}
        query = f"source_type={resource_type}&source_id=wf-shared".encode()
    elif entry in {"execution", "events"}:
        path = f"/api/v1/workflow-executions/{execution_id}"
        if entry == "events":
            path += "/events"
        params = {"execution_id": execution_id}
    request = Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": query,
        "path_params": params,
    })
    auth = AuthContext(
        user_id=str(recipient_user_id),
        tenant_id=str(recipient_tenant_id),
        email="recipient@example.test",
        active_organization_id=str(recipient_tenant_id),
        membership_status="active",
    )
    async with session_scope(
        tenant_id=str(recipient_tenant_id),
        user_id=str(recipient_user_id),
    ) as session:
        if entry == "cli":
            from vibecanvas_api.services.agent_runtime.resource_routes import admitted_resource_route_params
            context = SimpleNamespace(tenant_id=str(recipient_tenant_id), username=str(recipient_user_id),
                                      turn_id="test", authorization_client=object())
            params_out = await admitted_resource_route_params(context, session, resource_type, "wf-shared")
            request = params_out["request"]
            assert params_out["ctx"].user_id == str(recipient_user_id)
            assert params_out["ctx"].active_organization_id == str(recipient_tenant_id)
        else:
            await _admit_shared_resource(request, auth, session)
        current_tenant = (
            await session.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            )
        ).scalar_one()

    if entry == "history" and resource_type == "workflow":
        assert current_tenant == str(recipient_tenant_id)
        assert not hasattr(request.state, "admitted_resource_id")
    else:
        assert current_tenant == str(owner_id)
        assert request.state.admitted_resource_organization_id == str(owner_id)
        assert request.state.admitted_resource_type == resource_type
        assert request.state.admitted_resource_id == "wf-shared"

    # A known execution ID must not widen RLS after the discovery projection
    # is removed. The route's FGA check independently covers stale projections.
    async with pg_engine.begin() as connection:
        await connection.execute(text(
            "DELETE FROM shared_resource_projections WHERE source_mutation_id=:id"
        ), {"id": mutation_id})
    revoked_request = Request({
        "type": "http", "method": "GET", "path": path, "headers": [],
        "query_string": query, "path_params": params,
    })
    async with session_scope(tenant_id=str(recipient_tenant_id),
                             user_id=str(recipient_user_id)) as session:
        await _admit_shared_resource(revoked_request, auth, session)
        assert (await session.execute(text(
            "SELECT current_setting('app.tenant_id', true)"
        ))).scalar_one() == str(recipient_tenant_id)
    assert not hasattr(revoked_request.state, "admitted_resource_id")
