import pytest

from tests.test_chat_repo_pg import _seed_and_bind, USER, TENANT
from vibecanvas_api.security.canvas_workspace_migration import migrate_canvas_project_files
from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.object_store import InMemoryObjectStore
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from vibecanvas_api.storage.vfs_store import VfsRepo


async def fixture(db):
    await _seed_and_bind(db)
    wf = 'migration-workflow'
    await WorkflowRepo(db, str(USER)).create_workflow(wf_id=wf, name='Migration', initial_workflow={})
    project = await ChatProjectRepo(db, str(USER)).for_workflow(wf)
    chat = await ChatRepo(db, str(USER)).register_session(wf, project_id=project.project_id)
    store = InMemoryObjectStore()
    repo = VfsRepo(db, object_store=store)
    paths = [f'/chats/{chat}/attachment.txt', '/data/input.csv', '/logs/output.txt']
    for path in paths + ['/chats/someone-else/secret.txt', '/run/result.txt']:
        await repo.upsert_internal_artifact_bytes(wf_id=wf, tenant=str(TENANT), path=path,
            data=path.encode(), content_type='text/plain', abstract='Preserved metadata')
    await repo.write_scratch_bytes(wf_id=wf, tenant=str(TENANT), path='/memory/note.txt',
        data=b'private note', content_type='text/plain', abstract='Memory')
    return wf, project.project_id, store, repo, paths + ['/memory/note.txt']


@pytest.mark.asyncio
async def test_canvas_file_migration_is_private_idempotent_and_preserves_metadata(pg_session):
    wf, project, store, repo, paths = await fixture(pg_session)
    scope = project_workspace_scope_id(project)
    assert await migrate_canvas_project_files(pg_session, store, project_id=project) == 4
    assert await repo.read_bytes(wf_id=scope, path=paths[0]) is None
    assert await migrate_canvas_project_files(pg_session, store, project_id=project, apply=True) == 4
    for path in paths:
        assert await repo.read_bytes(wf_id=wf, path=path) is None
        assert await repo.read_bytes(wf_id=scope, path=path) == (b'private note' if path.startswith('/memory') else path.encode())
        assert (await repo.read(wf_id=scope, path=path)).abstract
    for path in ['/chats/someone-else/secret.txt', '/run/result.txt']:
        assert await repo.read_bytes(wf_id=scope, path=path) is None
        assert await repo.read_bytes(wf_id=wf, path=path) == path.encode()
    assert await migrate_canvas_project_files(pg_session, store, project_id=project, apply=True) == 0


@pytest.mark.asyncio
async def test_canvas_file_migration_rejects_conflicting_destination(pg_session):
    wf, project, store, repo, paths = await fixture(pg_session)
    scope = project_workspace_scope_id(project)
    await repo.upsert_internal_artifact_bytes(wf_id=scope, tenant=str(TENANT), path=paths[0],
        data=b'newer contents', content_type='text/plain')
    with pytest.raises(ValueError, match='destination_conflict'):
        async with pg_session.begin_nested():
            await migrate_canvas_project_files(pg_session, store, project_id=project, apply=True)
    assert await repo.read_bytes(wf_id=scope, path=paths[0]) == b'newer contents'
    assert await repo.read_bytes(wf_id=wf, path=paths[0]) == paths[0].encode()


def runtime_fixture():
    from vibecanvas_api.services.vfs_volume import EncryptedObjectStoreProjectRuntimeVolumeProvider
    store = InMemoryObjectStore()
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    identity = dict(tenant_id='tenant', user_id='owner')
    source = provider._coordinates(**identity, project_scope_id='workflow')[3] + '/'
    target = provider._coordinates(**identity, project_scope_id=project_workspace_scope_id('project'))[3] + '/'
    other = provider._coordinates(tenant_id='tenant', user_id='other', project_scope_id='workflow')[3] + '/'
    store.put_bytes(source + '.codex/sessions/thread.jsonl', b'private transcript')
    store.put_bytes(source + '.codex/state.sqlite', b'sqlite fixture')
    store.put_bytes(source + '.codex/auth.json', b'credential fixture')
    store.put_bytes(other + '.codex/sessions/thread.jsonl', b'other user')
    return store, source, target, other


def test_runtime_migration_retains_history_not_credentials_and_is_idempotent():
    from vibecanvas_api.security.canvas_workspace_migration import migrate_canvas_runtime
    store, source, target, other = runtime_fixture()
    args = dict(tenant_id='tenant', user_id='owner', workflow_id='workflow', project_id='project')
    assert migrate_canvas_runtime(store, **args) == 2
    assert list(store.list_keys(target)) == []
    assert migrate_canvas_runtime(store, **args, apply=True) == 2
    assert store.fetch_bytes(target + '.codex/sessions/thread.jsonl') == b'private transcript'
    assert store.fetch_bytes(target + '.codex/state.sqlite') == b'sqlite fixture'
    assert target + '.codex/auth.json' not in store.list_keys(target)
    assert list(store.list_keys(source)) == []
    assert store.fetch_bytes(other + '.codex/sessions/thread.jsonl') == b'other user'
    assert migrate_canvas_runtime(store, **args, apply=True) == 0


def test_runtime_migration_conflict_preserves_source_and_destination():
    from vibecanvas_api.security.canvas_workspace_migration import migrate_canvas_runtime
    store, source, target, _ = runtime_fixture()
    store.put_bytes(target + '.codex/state.sqlite', b'newer state')
    with pytest.raises(ValueError, match='destination_conflict'):
        migrate_canvas_runtime(store, tenant_id='tenant', user_id='owner', workflow_id='workflow', project_id='project', apply=True)
    assert store.fetch_bytes(source + '.codex/state.sqlite') == b'sqlite fixture'
    assert store.fetch_bytes(target + '.codex/state.sqlite') == b'newer state'
    assert target + '.codex/sessions/thread.jsonl' not in store.list_keys(target)


def test_runtime_migration_reopens_sqlite_from_encrypted_storage(tmp_path):
    import sqlite3
    from pathlib import Path
    from vibecanvas_api.security.canvas_workspace_migration import migrate_canvas_runtime
    from vibecanvas_api.services.object_store import FilesystemObjectStore
    from vibecanvas_api.services.vfs_volume import EncryptedObjectStoreProjectRuntimeVolumeProvider
    store = FilesystemObjectStore(root=str(tmp_path / 'encrypted'),
        materialized_root=str(tmp_path / 'materialized'), master_key=b'M' * 32)
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    identity = dict(tenant_id='tenant', user_id='owner')
    volume = provider.ensure(**identity, project_scope_id='workflow')
    home = Path(volume.path) / '.codex'
    home.mkdir()
    with sqlite3.connect(home / 'state.sqlite') as db:
        db.execute('CREATE TABLE threads (id TEXT, content TEXT)')
        db.execute("INSERT INTO threads VALUES ('thread-one', 'resume existing conversation')")
    (home / 'auth.json').write_text('private account fixture')
    provider.release(volume)
    assert migrate_canvas_runtime(store, **identity, workflow_id='workflow', project_id='project', apply=True) == 1
    restored = provider.ensure(**identity, project_scope_id=project_workspace_scope_id('project'))
    try:
        with sqlite3.connect(Path(restored.path) / '.codex/state.sqlite') as db:
            assert db.execute('SELECT id, content FROM threads').fetchone() == ('thread-one', 'resume existing conversation')
        assert not (Path(restored.path) / '.codex/auth.json').exists()
    finally:
        provider.release(restored)
