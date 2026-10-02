import json
import pytest
from sqlalchemy import text
from tests.test_deployment_rollout import setup_rollout
from vibecanvas_api.services.deployment_revisions import desired_key
from vibecanvas_api.services.pin_resource_versions import pin_resource_versions
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


@pytest.mark.asyncio
async def test_legacy_migration_keeps_serving_version_and_is_idempotent(pg_engine, app_engine):
    _, dep, spec = await setup_rollout(pg_engine, app_engine)
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        dep.update(version_pin='head', pinned_major=None, pinned_sub=None)
        spec['desired_key'] = desired_key(dep)
        await db.execute(text("UPDATE deployments SET version_pin='head', pinned_major=NULL, pinned_sub=NULL WHERE id=:id"), {'id': dep['id']})
        await db.execute(text('UPDATE deployment_runtime_revisions SET spec=CAST(:spec AS jsonb) WHERE id=:id'),
                         {'id': dep['active_revision_id'], 'spec': json.dumps(spec)})
        await WorkflowRepo(db, str(dep['user_id'])).commit(dep['wf_id'], {'marker': 'new'}, target_major=1)
        preview = await pin_resource_versions(db)
        assert preview == [{'kind': 'deployment', 'id': str(dep['id']), 'version': 'v1.sv0'}]
        assert await db.scalar(text('SELECT version_pin FROM deployments WHERE id=:id'), {'id': dep['id']}) == 'head'
        assert await pin_resource_versions(db, apply=True) == preview
        assert await pin_resource_versions(db, apply=True) == []
        pinned = dict((await db.execute(text('SELECT * FROM deployments WHERE id=:id'), {'id': dep['id']})).mappings().one())
        revision = (await db.execute(text('SELECT spec FROM deployment_runtime_revisions WHERE id=:id'), {'id': dep['active_revision_id']})).scalar_one()
        assert pinned['active_revision_id'] == dep['active_revision_id']
        assert pinned['pinned_sub'] == 0
        assert revision['desired_key'] == desired_key(pinned)
