"""Execution-scoped artifact reads share the history policy across HTTP surfaces."""

from dataclasses import replace
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import text

from tests.storage.test_workflow_history import owner, waiting_run
from tests.test_openfga_authz_service import _FakeClient
from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.authorization.dependencies import get_authz_service
from vibecanvas_api.authorization.openfga import OpenFgaAuthzService
from vibecanvas_api.authorization.types import (
    Action,
    AuthorizationCheck,
    AuthzRequestContext,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.routes import previews, vfs
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["workflow", "task", "deployment"])
async def test_history_artifact_policy_is_read_only_and_requires_assignment_or_inspect(pg_engine, source):
    tenant, actor, _ = await owner()
    execution, _, _ = await waiting_run(tenant, actor)
    client = _FakeClient()
    context = AuthzRequestContext(active_organization_id=tenant, membership_status="active")
    principal = PrincipalRef(PrincipalType.USER, actor)
    resource = ResourceRef(ResourceType.VFS_RUN, execution, tenant)
    source_id = "wf-test" if source == "workflow" else str(uuid4())
    async with session_scope(tenant_id=tenant) as session:
        await session.execute(
            text("UPDATE workflow_execution_runs SET source_type=:source, source_id=:source_id WHERE id=:id"),
            {"source": source, "source_id": source_id, "id": UUID(execution)},
        )
        service = OpenFgaAuthzService(session, client)
        decision = await service.check(principal, Action.VIEW, resource, context)
        assert decision.allowed and decision.capabilities == frozenset({Action.VIEW})
        assert not client.batch_calls
        checks = [
            AuthorizationCheck(principal, action, resource, context)
            for action in (Action.VIEW, Action.UPDATE, Action.DELETE, Action.EXECUTE)
        ]
        assert [item.allowed for item in await service.batch_check(checks)] == [True, False, False, False]
        assert not (
            await service.check(principal, Action.VIEW, resource, replace(context, membership_status="suspended"))
        ).allowed
        assert not (
            await service.check(principal, Action.VIEW, resource, replace(context, active_organization_id=str(uuid4())))
        ).allowed
        outsider = PrincipalRef(PrincipalType.USER, str(uuid4()))
        assert not (await service.check(outsider, Action.VIEW, resource, context)).allowed
        client.allowed_relations = {"can_view"}
        assert not (await service.check(outsider, Action.VIEW, resource, context)).allowed
        client.allowed_relations = {"can_inspect_runs"}
        assert (await service.check(outsider, Action.VIEW, resource, context)).allowed
        assert not (await service.check(outsider, Action.UPDATE, resource, context)).allowed
        assert all(check[2] == f"{source}:{source_id}" for calls, _ in client.batch_calls for check in calls)


@pytest.mark.asyncio
async def test_assignee_can_read_sign_and_preview_only_this_executions_artifacts(pg_engine):
    tenant, actor, email = await owner()
    execution, _, _ = await waiting_run(tenant, actor)
    other_tenant, other_actor, _ = await owner()
    other_execution, _, _ = await waiting_run(other_tenant, other_actor)
    auth = AuthContext(user_id=actor, tenant_id=tenant, email=email, membership_status="active")
    async with session_scope(tenant_id=tenant) as session:
        repo = VfsRunRepo(session, get_object_store(), tenant)
        for run in (execution, "unrelated-workflow-run"):
            await repo.write_bytes(
                run_id=run,
                path="/run/report.txt",
                data=b"approval evidence",
                content_type="text/plain",
                wf_id="wf-test",
            )
        await repo.write_bytes(
            run_id=execution, path="/run/private.txt", data=b"separate file", content_type="text/plain", wf_id="wf-test"
        )
    app = FastAPI()
    app.include_router(vfs.router)
    app.include_router(previews.router)

    async def user():
        return auth

    async def db():
        async with session_scope(tenant_id=tenant) as session:
            yield session

    async def service(session=Depends(tenant_db)):
        return OpenFgaAuthzService(session, _FakeClient())

    app.dependency_overrides.update({current_user: user, tenant_db: db, get_authz_service: service})
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        listing = await client.get(f"/api/v1/vfs/runs/{execution}")
        assert listing.status_code == 200, listing.text
        content = await client.get("/api/v1/vfs/content", params={"path": "/run/report.txt", "run_id": execution})
        assert content.status_code == 200 and content.json()["content"] == "approval evidence"
        signed = await client.post("/api/v1/vfs/sign", json={"path": "/run/report.txt", "run_id": execution})
        assert signed.status_code == 200, signed.text
        raw = await client.get(signed.json()["url"])
        assert raw.status_code == 200 and raw.content == b"approval evidence"
        file_ref = {"schemaVersion": 1, "scope": "run", "runId": execution, "path": "/run/report.txt"}
        preview = await client.post("/api/v1/previews/resolve", json={"fileRef": file_ref})
        assert preview.status_code == 200, preview.text
        resources = await client.post("/api/v1/previews/resource-session", json={"fileRef": file_ref})
        assert resources.status_code == 200, resources.text
        root = resources.json()["resourceMounts"][0]["rootUrl"]
        assert (await client.get(root + "report.txt")).content == b"approval evidence"
        assert (await client.get(root + "private.txt")).status_code == 403
        for denied_run in ("unrelated-workflow-run", other_execution):
            denied = await client.post("/api/v1/vfs/sign", json={"path": "/run/report.txt", "run_id": denied_run})
            assert denied.status_code == 404
            denied = await client.post(
                "/api/v1/previews/resource-session", json={"fileRef": {**file_ref, "runId": denied_run}}
            )
            assert denied.status_code == 404
