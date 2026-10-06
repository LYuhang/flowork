from pathlib import Path

import pytest

from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity


@pytest.mark.parametrize('kind', ['project', 'workflow', 'task', 'deployment', 'user_mount'])
def test_persistent_resource_is_shared_across_worker_lifetimes(tmp_path, kind):
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('tenant', kind, 'resource')
    first = storage.acquire(identity)
    Path(first.directory, 'file').write_text('first')
    second = storage.acquire(identity)
    assert second.directory == first.directory
    Path(second.directory, 'file').write_text('second')
    storage.release(first)
    storage.release(second)
    restored = PosixWorkspaceStorage(str(tmp_path)).acquire(identity)
    assert Path(restored.directory, 'file').read_text() == 'second'


def test_resource_scope_and_explicit_delete(tmp_path):
    storage = PosixWorkspaceStorage(str(tmp_path))
    identities = [WorkspaceIdentity('t', 'task', 'a'), WorkspaceIdentity('t', 'deployment', 'a'),
                  WorkspaceIdentity('other', 'task', 'a'), WorkspaceIdentity('t', 'task', 'b')]
    bindings = [storage.acquire(identity) for identity in identities]
    assert len({b.directory for b in bindings}) == 4
    for b in bindings:
        Path(b.directory, 'data').write_text('keep')
    assert storage.delete(identities[0])
    assert all(Path(b.directory, 'data').exists() for b in bindings[1:])


def test_symlink_cannot_redirect_resource_directory(tmp_path):
    storage = PosixWorkspaceStorage(str(tmp_path / 'root'))
    identity = WorkspaceIdentity('tenant', 'task', 'resource')
    binding = storage.acquire(identity)
    Path(binding.directory).rmdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('private')
    Path(binding.directory).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        storage.acquire(identity)
    with pytest.raises(ValueError):
        storage.delete(identity)
    assert (outside / 'keep').read_text() == 'private'


@pytest.mark.asyncio
async def test_deployment_owners_share_run_and_release_preserves_it(tmp_path, monkeypatch):
    import uuid
    from vibecanvas_api.services import deployment_workspace as module
    monkeypatch.setattr(module.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(module.config, 'workspace_storage_root', str(tmp_path))
    tenant, deployment = str(uuid.uuid4()), str(uuid.uuid4())
    pool = module.DeploymentWorkspaces()
    first = await pool.acquire(tenant, deployment, 'old-revision')
    second = await pool.acquire(tenant, deployment, 'new-revision')
    assert first.root == second.root
    (first.root / 'shared').write_text('preserve')
    await pool.release(tenant, deployment, 'old-revision')
    assert (second.root / 'shared').read_text() == 'preserve'
    await pool.release(tenant, deployment, 'new-revision')
    new_pool = module.DeploymentWorkspaces()
    restored = await new_pool.acquire(tenant, deployment, 'after-restart')
    assert (restored.root / 'shared').read_text() == 'preserve'
    await new_pool.release(tenant, deployment, 'after-restart')


@pytest.mark.asyncio
async def test_user_mount_shared_across_async_sync_and_release(tmp_path, monkeypatch):
    from vibecanvas_api.services import user_mount_workspace as module
    monkeypatch.setattr(module.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(module.config, 'workspace_storage_root', str(tmp_path))
    args = dict(user_id='user', tenant_id='tenant')
    first = await module.create_user_mount(**args)
    second = module.create_user_mount_sync(**args)
    assert first == second
    Path(first, 'shared').write_text('durable')
    assert await module.persist_user_mount(source=first, **args) == 0
    assert module.persist_user_mount_sync(source=second, **args) == 0
    module.remove_user_mount(first)
    assert Path(second, 'shared').read_text() == 'durable'
    other = await module.create_user_mount(user_id='other', tenant_id='tenant')
    assert other != first
    with pytest.raises(ValueError):
        await module.persist_user_mount(source=other, **args)


def test_range_read_uses_latest_file_and_stays_inside_resource(tmp_path):
    import os
    storage = PosixWorkspaceStorage(str(tmp_path / 'store'))
    identity = WorkspaceIdentity('tenant', 'task', 'task')
    binding = storage.acquire(identity)
    root = Path(binding.directory)
    (root / 'result').write_bytes(b'0123456789')
    assert list(storage.iter_bytes(identity, 'result', start=2, end=6, chunk_size=2)) == [b'23', b'45', b'6']
    (root / 'result').write_bytes(b'updated')
    assert b''.join(storage.iter_bytes(identity, 'result')) == b'updated'
    for path in ['../secret', '/etc/passwd', 'a/../result', '', 'a//b']:
        with pytest.raises(ValueError):
            list(storage.iter_bytes(identity, path))
    outside = tmp_path / 'secret'
    outside.write_bytes(b'private')
    (root / 'escape').symlink_to(outside)
    with pytest.raises(ValueError, match='symbolic link'):
        list(storage.iter_bytes(identity, 'escape'))
    (root / 'parent').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        list(storage.iter_bytes(identity, 'parent/secret'))
    os.mkfifo(root / 'pipe')
    with pytest.raises(ValueError):
        list(storage.iter_bytes(identity, 'pipe'))


@pytest.mark.asyncio
@pytest.mark.parametrize('expose_run', [False, True])
async def test_manager_posix_workspace_survives_session_close(tmp_path, monkeypatch, expose_run):
    from unittest.mock import MagicMock
    from vibecanvas_api.services.sandbox import manager as module
    monkeypatch.setattr(module.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(module.config, 'workspace_storage_root', str(tmp_path / 'volumes'))
    monkeypatch.setattr(module.config, 'agent_overlay_root', str(tmp_path / 'overlays'))
    monkeypatch.setattr(module, 'get_sandbox_provider', lambda: MagicMock())
    monkeypatch.setattr(module, '_workflow_python_binds', lambda: [])
    def forbidden(*args, **kwargs):
        raise AssertionError('persistent workspace must not hydrate old objects')
    monkeypatch.setattr(module, 'build_run_context', forbidden)
    manager = module.SandboxManager(max_resident=2, idle_ttl_s=600)
    first = await manager._build_session('tenant', 'resource', expose_run=expose_run, expose_mount=False)
    Path(first.run_dir, 'data', 'file').write_text('retained')
    original = first.run_dir
    await first.close()
    assert Path(original, 'data', 'file').read_text() == 'retained'
    second = await manager._build_session('tenant', 'resource', expose_run=expose_run, expose_mount=False)
    assert second.run_dir == original
    assert Path(second.run_dir, 'data', 'file').read_text() == 'retained'
    await second.close()


def test_clear_preserves_selected_folders_and_mounted_root_inode(tmp_path):
    storage = PosixWorkspaceStorage(str(tmp_path / 'store'))
    identity = WorkspaceIdentity('tenant', 'workflow', 'workflow')
    root = Path(storage.acquire(identity).directory)
    inode = root.stat().st_ino
    (root / 'chats').mkdir()
    (root / 'chats/history').write_text('keep')
    (root / 'output').mkdir()
    (root / 'output/result').write_text('remove')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'file').write_text('private')
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    storage.clear(identity, preserve=frozenset({'chats'}))
    assert root.stat().st_ino == inode
    assert (root / 'chats/history').read_text() == 'keep'
    assert not (root / 'output').exists()
    assert not (root / 'escape').exists()
    assert (outside / 'file').read_text() == 'private'


@pytest.mark.asyncio
async def test_stream_stages_job_outside_persistent_run(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from vibecanvas_api.services.sandbox.manager import SandboxSession
    from vibecanvas_api.services import workflow_sandbox_runner

    storage = PosixWorkspaceStorage(str(tmp_path / 'storage'))
    binding = storage.acquire(WorkspaceIdentity('tenant', 'workflow', 'workflow-id'))
    session = SandboxSession.__new__(SandboxSession)
    session.tenant_id = 'tenant'
    session.workflow_run_id = 'workflow-id'
    session.workflow_run_dir = binding.directory
    session.persistent_run_binding = binding
    session.pool_runs_root = str(tmp_path / 'private' / 'runs')
    session._workflow_job_lock = asyncio.Lock()
    session._begin_activity = Mock()
    session._end_activity = Mock()
    pool = Mock()
    async def stream(**kwargs):
        yield {'type': 'done'}
    pool.submit_stream = stream
    session._get_fileop_pool = AsyncMock(return_value=pool)
    stage = Mock()
    monkeypatch.setattr(workflow_sandbox_runner, 'stage_workflow_job', stage)
    events = [event async for event in session.submit_workflow_stream(
        workflow={}, inputs={}, run_id='workflow-id', tenant='tenant')]
    assert events == [{'type': 'done'}]
    assert stage.call_args.args[0] == session.pool_runs_root
    pool.release_egress_hosts.assert_called_once()
    session._end_activity.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,target', [('task', 'resource'), ('workflow', 'other')])
async def test_workflow_clear_rejects_wrong_resource(tmp_path, kind, target):
    from unittest.mock import Mock
    from vibecanvas_api.services.sandbox.manager import SandboxSession
    storage = PosixWorkspaceStorage(str(tmp_path))
    binding = storage.acquire(WorkspaceIdentity('tenant', kind, 'resource'))
    marker = Path(binding.directory, 'keep')
    marker.write_text('unchanged')
    session = SandboxSession.__new__(SandboxSession)
    session.persistent_run_binding = binding
    session._begin_activity = Mock()
    session._end_activity = Mock()
    with pytest.raises(ValueError, match='does not match'):
        await session.clear_workflow_run(target)
    assert marker.read_text() == 'unchanged'
    session._end_activity.assert_called_once()


def test_live_entries_reflect_writes_and_prune_private_chats(tmp_path):
    from vibecanvas_api.services.workspace_file_access import workspace_path_visible
    storage = PosixWorkspaceStorage(str(tmp_path / 'storage'))
    identity = WorkspaceIdentity('tenant', 'workflow', 'workflow')
    root = Path(storage.acquire(identity).directory)
    for name in ('chats/mine', 'chats/other', 'data/empty'):
        (root / name).mkdir(parents=True)
    (root / 'chats/mine/message').write_text('mine')
    (root / 'chats/other/private').write_text('private')
    file = root / 'data/result'
    file.write_text('first')
    visible = lambda path: workspace_path_visible(path, {'mine'})
    entries = {e.path: e for e in storage.entries(identity, visible=visible)}
    assert 'chats/other' not in entries
    assert 'chats/other/private' not in entries
    assert entries['chats/mine/message'].size_bytes == 4
    assert entries['data/empty'].is_directory
    assert entries['data/result'].size_bytes == 5
    file.write_text('updated result')
    assert {e.path: e for e in storage.entries(identity)}['data/result'].size_bytes == 14
    file.unlink()
    assert 'data/result' not in {e.path for e in storage.entries(identity)}


def test_entries_never_follow_links_or_read_file_bodies(tmp_path):
    import os
    storage = PosixWorkspaceStorage(str(tmp_path / 'storage'))
    identity = WorkspaceIdentity('tenant', 'project', 'project')
    assert list(storage.entries(identity)) == []
    root = Path(storage.acquire(identity).directory)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('secret')
    (root / 'external').symlink_to(outside, target_is_directory=True)
    (root / 'external-file').symlink_to(outside / 'secret')
    os.mkfifo(root / 'pipe')
    with (root / 'large').open('wb') as target:
        target.truncate(1024 ** 3)
    entries = list(storage.entries(identity))
    assert len(entries) == 1
    assert entries[0].path == 'large'
    assert entries[0].size_bytes == 1024 ** 3


def test_atomic_write_failure_preserves_previous_content(tmp_path):
    import io
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('tenant', 'task', 'task')
    assert not storage.write_file(identity, 'nested/result', io.BytesIO(b'original'))
    class BrokenSource:
        def read(self, size):
            raise OSError('source interrupted')
    with pytest.raises(OSError, match='source interrupted'):
        storage.write_file(identity, 'nested/result', BrokenSource())
    assert b''.join(storage.iter_bytes(identity, 'nested/result')) == b'original'
    assert not any('.workspace-write-' in e.path for e in storage.entries(identity))
    assert storage.write_file(identity, 'nested/result', io.BytesIO(b'replaced'))
    assert b''.join(storage.iter_bytes(identity, 'nested/result')) == b'replaced'


def test_write_rejects_link_targets_and_link_parents(tmp_path):
    import io
    storage = PosixWorkspaceStorage(str(tmp_path / 'storage'))
    identity = WorkspaceIdentity('tenant', 'task', 'task')
    root = Path(storage.acquire(identity).directory)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_bytes(b'keep')
    (root / 'link').symlink_to(outside / 'secret')
    (root / 'parent').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        storage.write_file(identity, 'link', io.BytesIO(b'change'))
    with pytest.raises(NotADirectoryError):
        storage.write_file(identity, 'parent/secret', io.BytesIO(b'change'))
    assert (outside / 'secret').read_bytes() == b'keep'


def test_rename_and_remove_shared_subtree_without_touching_other_resource(tmp_path):
    import io
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('tenant', 'task', 'task')
    other = WorkspaceIdentity('tenant', 'task', 'other')
    for resource in (identity, other):
        storage.write_file(resource, 'data/folder/a', io.BytesIO(b'a'))
        storage.write_file(resource, 'data/folder/b', io.BytesIO(b'b'))
    storage.rename_path(identity, 'data/folder', 'data/moved')
    assert b''.join(storage.iter_bytes(identity, 'data/moved/a')) == b'a'
    assert storage.remove_path(identity, 'data/moved') == 2
    assert storage.remove_path(identity, 'data/moved') == 0
    assert b''.join(storage.iter_bytes(other, 'data/folder/a')) == b'a'


def test_remove_and_rename_do_not_follow_external_directory_links(tmp_path):
    storage = PosixWorkspaceStorage(str(tmp_path / 'storage'))
    identity = WorkspaceIdentity('tenant', 'workflow', 'workflow')
    root = Path(storage.acquire(identity).directory)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    (root / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(NotADirectoryError):
        storage.remove_path(identity, 'link/keep')
    with pytest.raises(NotADirectoryError):
        storage.rename_path(identity, 'link/keep', 'moved')
    storage.rename_path(identity, 'link', 'renamed-link')
    assert storage.remove_path(identity, 'renamed-link') == 1
    assert (outside / 'keep').read_text() == 'keep'
    with pytest.raises(ValueError):
        storage.remove_path(identity, '../outside')


def test_personal_tenant_erasure_preserves_organization_resources(tmp_path):
    import io
    storage = PosixWorkspaceStorage(str(tmp_path))
    personal = WorkspaceIdentity('personal', 'project', 'project')
    organization = WorkspaceIdentity('organization', 'workflow', 'workflow')
    own_mount = WorkspaceIdentity('organization', 'user_mount', 'user')
    other_mount = WorkspaceIdentity('organization', 'user_mount', 'other')
    for identity in (personal, organization, own_mount, other_mount):
        storage.write_file(identity, 'keep', io.BytesIO(b'content'))
    assert storage.delete_tenant('personal')
    assert storage.delete(own_mount)
    assert not storage.delete_tenant('personal')
    assert b''.join(storage.iter_bytes(organization, 'keep')) == b'content'
    assert b''.join(storage.iter_bytes(other_mount, 'keep')) == b'content'


def test_nested_write_preserves_service_group_access_under_standard_umask(tmp_path):
    import io
    import os
    import stat
    storage = PosixWorkspaceStorage(str(tmp_path / 'storage'))
    identity = WorkspaceIdentity('tenant', 'task', 'task')
    previous_umask = os.umask(0o022)
    try:
        storage.write_file(identity, 'nested/deeper/file', io.BytesIO(b'data'))
    finally:
        os.umask(previous_umask)
    root = Path(storage.acquire(identity).directory)
    for directory in [root, root / 'nested', root / 'nested/deeper']:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o2770


def test_failed_temporary_open_preserves_original_error(tmp_path, monkeypatch):
    import io
    import os
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('tenant', 'task', 'task')
    storage.acquire(identity)
    original_open = os.open
    def denied(path, flags, *args, **kwargs):
        if str(path).startswith('.workspace-write-'):
            raise PermissionError('write denied')
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', denied)
    with pytest.raises(PermissionError, match='write denied'):
        storage.write_file(identity, 'file', io.BytesIO(b'data'))
