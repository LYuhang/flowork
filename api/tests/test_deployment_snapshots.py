import uuid

import pytest
from fastapi import HTTPException

from tests.test_deployments_create import _seed_tenant_and_user, _seed_workflow, _seed_workflow_version
from vibecanvas_api.services.deployment_snapshots import resolve_workflow
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


@pytest.mark.asyncio
async def test_branch_latest_fixed_and_missing_versions(pg_engine, app_engine):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    wf_id = "dep-" + uuid.uuid4().hex[:12]
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)
    await _seed_workflow_version(app_engine, wf_id, tenant_id, user_id, major=2, sub=2)
    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).commit(wf_id, {}, target_major=1, note="branch 1")
        async def version(policy, major=None, sub=None):
            graph = await resolve_workflow(session, user_id, {"wf_id": wf_id, "version_pin": policy,
                "pinned_major": major, "pinned_sub": sub})
            meta = graph["__meta__"]
            return meta["workflow_version"], meta["workflow_subversion"]
        assert await version("major", 1) == (1, 1)
        assert await version("specific", 1, 0) == (1, 0)
        assert await version("major", 2) == (2, 2)
        assert await version("head") == (2, 2)  # legacy highest-major policy
        with pytest.raises(HTTPException):
            await version("major", 9)
        with pytest.raises(HTTPException):
            await version("specific", 1, 99)
