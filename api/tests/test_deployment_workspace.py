import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.deployment_workspace import DeploymentWorkspaces
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo
from vibecanvas_api.services.object_store import get_object_store


@pytest.mark.asyncio
async def test_deployment_files_survive_revision_changes_and_rebuild(pg_engine, app_engine):
    from tests.test_deployment_rollout import setup_rollout
    _, dep, _ = await setup_rollout(pg_engine, app_engine)
    tenant, deployment = str(dep['tenant_id']), str(dep['id'])
    registry = DeploymentWorkspaces()
    first = await registry.acquire(tenant, deployment, 'revision-a')
    try:
        (first.root / 'retained.txt').write_text('first call')
        (first.root / 'deleted.txt').write_text('delete me')
        await first.sync()
        second = await registry.acquire(tenant, deployment, 'revision-b')
        assert second is first
        (second.root / 'retained.txt').write_text('second call')
        (second.root / 'deleted.txt').unlink()
        await second.sync()
        # An unrelated deployment has a different /run, even for the same graph.
        other_id = str(uuid.uuid4())
        other = await registry.acquire(tenant, other_id, 'other')
        assert not (other.root / 'retained.txt').exists()
        await registry.release(tenant, other_id, 'other')
        await registry.release(tenant, deployment, 'revision-a')
        assert second.root.exists()
        old_root = second.root
        await registry.release(tenant, deployment, 'revision-b')
        assert not old_root.exists()
        rebuilt = await registry.acquire(tenant, deployment, 'revision-c')
        assert (rebuilt.root / 'retained.txt').read_text() == 'second call'
        assert not (rebuilt.root / 'deleted.txt').exists()
        async with short_session_scope(tenant_id=tenant) as db:
            repo = VfsRunRepo(db, get_object_store(), tenant)
            assert [entry.path for entry in await repo.ls(run_id=rebuilt.run_id)] == ['/run/retained.txt']
            # Canvas reruns must never purge Deployment files.
            await repo.purge_workflow_runs(wf_id=dep['wf_id'])
            assert await repo.read_bytes(run_id=rebuilt.run_id, path='/run/retained.txt') == b'second call'
        await registry.release(tenant, deployment, 'revision-c')
    finally:
        for workspace in registry.entries.values():
            workspace.directory.cleanup()


@pytest.mark.asyncio
async def test_failed_writeback_retains_projection_for_retry(monkeypatch):
    from vibecanvas_api.services.deployment_workspace import DeploymentWorkspace
    monkeypatch.setattr(DeploymentWorkspace, 'hydrate', AsyncMock())
    registry = DeploymentWorkspaces()
    tenant, deployment = str(uuid.uuid4()), str(uuid.uuid4())
    workspace = await registry.acquire(tenant, deployment, 'revision')
    workspace.sync = AsyncMock(side_effect=RuntimeError('storage unavailable'))
    with pytest.raises(RuntimeError, match='storage unavailable'):
        await registry.release(tenant, deployment, 'revision')
    assert workspace.root.exists() and workspace.owners == {'revision'}
    workspace.sync = AsyncMock()
    await registry.release(tenant, deployment, 'revision')
    assert not workspace.root.exists()
