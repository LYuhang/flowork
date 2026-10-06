"""Exercise lazy production imports even when a cleanup job is a no-op."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from vibecanvas_api.services.workflow_deletion import cleanup


@pytest.mark.asyncio
async def test_cleanup_without_ledger_is_safe_and_importable(monkeypatch):
    session = AsyncMock()
    session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=None))

    @asynccontextmanager
    async def admin_session():
        yield session

    monkeypatch.setattr(
        "vibecanvas_api.storage.sync_session.short_admin_session", admin_session
    )
    await cleanup("missing-cleanup-fixture")
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["releasing", "release_failed"])
async def test_unconfirmed_sandbox_release_preserves_all_durable_files(monkeypatch, lifecycle):
    from types import SimpleNamespace
    session = AsyncMock()
    session.execute.side_effect = [Mock(scalar_one_or_none=Mock(return_value="tenant")),
                                   Mock(one=Mock(return_value=SimpleNamespace(completed_at=None)))]
    session.get.return_value = SimpleNamespace(deleted_at=object())
    @asynccontextmanager
    async def admin_session():
        yield session
    manager = SimpleNamespace(close_session=AsyncMock(return_value={"lifecycle_state": lifecycle}))
    store = Mock()
    monkeypatch.setattr("vibecanvas_api.storage.sync_session.short_admin_session", admin_session)
    monkeypatch.setattr("vibecanvas_api.services.sandbox.manager.get_sandbox_manager", lambda: manager)
    monkeypatch.setattr("vibecanvas_api.services.object_store.get_object_store", lambda: store)
    with pytest.raises(RuntimeError, match="shutdown/persistence is not confirmed"):
        await cleanup("workflow")
    assert session.execute.await_count == 2
    session.delete.assert_not_awaited()
    store.delete_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_posix_cleanup_removes_workflow_and_linked_private_project(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    from vibecanvas_api.config import config
    from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity
    from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
    monkeypatch.setattr(config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(config, 'workspace_storage_root', str(tmp_path))
    scope = project_workspace_scope_id('project')
    storage = PosixWorkspaceStorage(str(tmp_path))
    identities = [WorkspaceIdentity('tenant', 'workflow', 'workflow'),
                  WorkspaceIdentity('private-tenant', 'project', scope),
                  WorkspaceIdentity('tenant', 'workflow', 'other')]
    for identity in identities:
        storage.write_file(identity, 'keep', io.BytesIO(b'content'))
    project = SimpleNamespace(project_id='project', tenant_id='private-tenant', creator_user_id='user')
    rows = lambda values: Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=values))))
    session = AsyncMock()
    session.execute.side_effect = [Mock(scalar_one_or_none=Mock(return_value='tenant')),
        Mock(one=Mock(return_value=SimpleNamespace(completed_at=None))), rows([project]),
        rows([]), rows([]), rows([]), Mock()]
    session.get.return_value = SimpleNamespace(deleted_at=object())
    @asynccontextmanager
    async def admin_session():
        yield session
    manager = SimpleNamespace(close_session=AsyncMock(return_value={'lifecycle_state': 'closed'}))
    provider = Mock()
    monkeypatch.setattr('vibecanvas_api.storage.sync_session.short_admin_session', admin_session)
    monkeypatch.setattr('vibecanvas_api.services.sandbox.manager.get_sandbox_manager', lambda: manager)
    monkeypatch.setattr('vibecanvas_api.services.object_store.get_object_store', Mock())
    monkeypatch.setattr('vibecanvas_api.services.vfs_volume.get_project_runtime_volume_provider', lambda: provider)
    await cleanup('workflow')
    assert list(storage.entries(identities[0])) == []
    assert list(storage.entries(identities[1])) == []
    assert b''.join(storage.iter_bytes(identities[2], 'keep')) == b'content'
    manager.close_session.assert_any_await('private-tenant', scope)
    provider.delete.assert_called_once_with(tenant_id='private-tenant', user_id='user', project_scope_id=scope)
