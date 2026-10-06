from pathlib import Path

from vibecanvas_api.services.vfs_volume import LocalPosixProjectRuntimeVolumeProvider


def test_release_retains_data_and_explicit_delete_is_scoped(tmp_path):
    provider = LocalPosixProjectRuntimeVolumeProvider(str(tmp_path))
    ids = dict(tenant_id='tenant', user_id='user', project_scope_id='project')
    first = provider.ensure(**ids)
    file = Path(first.path) / 'result.txt'
    file.write_text('durable')
    other = provider.ensure(**{**ids, 'user_id': 'other'})
    (Path(other.path) / 'private.txt').write_text('private')
    assert provider.sync(first) == 0
    assert provider.release(first) == 0
    rebuilt = LocalPosixProjectRuntimeVolumeProvider(str(tmp_path))
    assert rebuilt.ensure(**ids).path == first.path
    assert file.read_text() == 'durable'
    assert rebuilt.delete(**ids)
    assert not file.exists()
    assert (Path(other.path) / 'private.txt').read_text() == 'private'
    assert not rebuilt.delete(**ids)


def test_factory_selects_posix_without_opening_object_store(tmp_path, monkeypatch):
    from vibecanvas_api.services import vfs_volume
    monkeypatch.setattr(vfs_volume.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(vfs_volume.config, 'workspace_storage_root', str(tmp_path))
    def forbidden():
        raise AssertionError('POSIX volume must not materialize objects')
    monkeypatch.setattr(vfs_volume, 'get_object_store', forbidden)
    provider = vfs_volume.get_project_runtime_volume_provider()
    volume = provider.ensure(tenant_id='tenant', user_id='user', project_scope_id='project')
    Path(volume.path, 'state').write_text('persisted')
    provider.release(volume)
    assert Path(volume.path, 'state').read_text() == 'persisted'
