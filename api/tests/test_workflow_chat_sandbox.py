"""Private canvas workspaces mount a separately authorized shared Workflow run."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.sandbox.manager import SandboxManager, SandboxSession


@pytest.mark.asyncio
async def test_workflow_and_chat_reuse_session_and_shared_run_and_chats(tmp_path):
    root = tmp_path / "workflow"
    (root / "chats" / "first").mkdir(parents=True)
    (root / "chats" / "first" / "notes.txt").write_text("shared notes")
    (root / "result.json").write_text('{"value": 42}')
    session = SandboxSession(
        tenant_id="tenant", wf_id="workflow", user_id="user", run_dir=str(root),
        runtime_dir=str(tmp_path / "runtime"), overlay_dir=None,
        provider=MagicMock(), base_binds=[], expose_run=True,
    )
    manager = SandboxManager(max_resident=1, idle_ttl_s=10)
    manager._build_session = AsyncMock(return_value=session)
    run = await manager.get_session("tenant", "workflow", user_id="user", expose_runtime=True)
    scope = "workflow"
    chat = await manager.get_session("tenant", scope, user_id="user", expose_runtime=True)
    assert chat is run
    manager._build_session.assert_awaited_once()
    mounts, _, _ = chat._agent_runtime_launch_spec(runtime_type="test", uses_codex_account=False)
    mounts = dict(mounts)
    assert mounts["/run"] == str(root)
    assert mounts["/chats"] == str(root / "chats")
    # Creating a second conversation does not replace or narrow the mount.
    assert await manager.mirror_vfs_write("tenant", scope, "/chats/second/notes.txt", b"second notes")
    assert (root / "chats/first/notes.txt").read_text() == "shared notes"
    assert (root / "chats/second/notes.txt").read_bytes() == b"second notes"
    assert (root / "result.json").exists()
    # A workflow grant cannot transfer the first user's runtime/mount identity.
    for other_user in ("other-user", None):
        with pytest.raises(RuntimeError, match="sandbox_workspace_identity_mismatch"):
            await manager.get_session("tenant", scope, user_id=other_user, expose_runtime=True)
    assert await manager.get_session("tenant", scope, user_id="user", expose_runtime=True) is run
    manager._build_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_workspaces_mount_and_persist_the_same_shared_run(tmp_path, monkeypatch):
    from vibecanvas_api.services.sandbox.manager import WorkflowRunBinding
    shared = tmp_path / 'shared'
    shared.mkdir()
    binding = WorkflowRunBinding('source-tenant', 'shared-workflow', str(shared))
    sessions = []
    for actor in ('alice', 'bob'):
        private = tmp_path / actor
        (private / 'chats').mkdir(parents=True)
        (private / 'chats' / 'private.txt').write_text(actor)
        sessions.append(SandboxSession(
            tenant_id=f'{actor}-tenant', wf_id=f'{actor}-workspace', user_id=actor,
            run_dir=str(private), runtime_dir=None, overlay_dir=None,
            provider=MagicMock(), base_binds=[], expose_run=True,
            workflow_run_binding=binding,
        ))
    first, second = sessions
    from vibecanvas_api.services.sandbox.workflow_rpc_pool import WorkflowRpcPool
    for item in sessions:
        pool = WorkflowRpcPool.for_session(session=item, revision='shared', workflow={}, capacity=1)
        try:
            worker = pool.factory(0)
            assert worker.artifacts == shared
            assert dict(worker.rw_binds)['/run/chats'] == str(tmp_path / item.user_id / 'chats')
        finally:
            await pool.close()
        assert shared.exists()
    for item in sessions:
        mounts, _, _ = item._agent_runtime_launch_spec(runtime_type='test', uses_codex_account=False)
        mounts = dict(mounts)
        assert mounts['/run'] == str(shared)
        assert mounts['/chats'] == str(tmp_path / item.user_id / 'chats')
        assert item._external_vfs_target('/run/chats/private.txt')[1] == str(tmp_path / item.user_id / 'chats/private.txt')
        assert item._external_vfs_target('/run/result.txt')[1] == str(shared / 'result.txt')
        assert item._external_vfs_target('/run/chats') is None
        assert str(tmp_path / ('bob' if item.user_id == 'alice' else 'alice')) not in mounts.values()
    (shared / 'result.txt').write_text('shared result')
    assert (tmp_path / 'alice/chats/private.txt').read_text() == 'alice'
    assert (tmp_path / 'bob/chats/private.txt').read_text() == 'bob'
    assert (shared / 'result.txt').read_text() == 'shared result'
    persist = AsyncMock()
    monkeypatch.setattr('vibecanvas_api.services.sandbox.manager.sync_run_back', persist)
    for item in sessions:
        item._sync_run_folder = AsyncMock()
        item._sync_mount_folder = AsyncMock()
        await item.writeback_vfs(strict=True)
    assert persist.await_count == 2
    persist.assert_awaited_with('shared-workflow', 'source-tenant', str(shared), 'shared-workflow')
    clear = AsyncMock()
    monkeypatch.setattr('vibecanvas_api.services.vfs_run_context.clear_run_contents', clear)
    await second.clear_workflow_run()
    clear.assert_awaited_once_with('shared-workflow', 'source-tenant')


@pytest.mark.skipif(__import__('os').environ.get('FLOWORK_TEST_SKILL_MOUNT') != '1' or not __import__('shutil').which('bwrap'),
                    reason='explicit native sandbox mount verification')
def test_native_private_chats_and_shared_run(tmp_path):
    import json
    import shutil
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
    from vibecanvas_api.services.sandbox.manager import WorkflowRunBinding
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'chats').mkdir()
    (shared / 'chats' / 'source-owner-secret.txt').write_text('must remain private')
    provider = BubblewrapProvider(shutil.which('bwrap'))
    for actor in ('alice', 'bob'):
        private = tmp_path / actor
        private.mkdir()
        work = tmp_path / f'{actor}-work'
        work.mkdir()
        runs = tmp_path / f'{actor}-runs'
        runs.mkdir()
        session = SandboxSession(
            tenant_id=actor, wf_id=f'{actor}-private', user_id=actor,
            run_dir=str(private), runtime_dir=None, overlay_dir=None,
            provider=provider, base_binds=[], expose_run=True,
            workflow_run_binding=WorkflowRunBinding('owner', 'shared-workflow', str(shared)),
        )
        for folder in session.workspace_folders:
            (private / folder).mkdir()
        (private / 'chats' / f'{actor}.txt').write_text(actor)
        mounts, _, _ = session._agent_runtime_launch_spec(runtime_type='test', uses_codex_account=False)
        script = '''
from pathlib import Path
import json
shared=Path('/run/result.txt')
previous=shared.read_text() if shared.exists() else None
own=sorted(p.name for p in Path('/chats').iterdir())
assert sorted(p.name for p in Path('/run/chats').iterdir()) == own
shared.write_text(own[0])
Path('/work/result.json').write_text(json.dumps({'previous':previous,'chats':own}))
'''
        handle = provider.run_serve(runs_root=str(runs), work_dir=str(work),
                                    extra_rw_binds=mounts, command=['/usr/bin/python3', '-c', script])
        try:
            _, error = handle.proc.communicate(timeout=20)
            assert handle.proc.returncode == 0, error
            observed = json.loads((work / 'result.json').read_text())
            assert observed == {'previous': None if actor == 'alice' else 'alice.txt', 'chats': [f'{actor}.txt']}
        finally:
            if handle.proc.poll() is None:
                handle.proc.kill()
                handle.proc.wait(timeout=5)
    assert (shared / 'result.txt').read_text() == 'bob.txt'

@pytest.mark.asyncio
async def test_native_canvas_worker_uses_shared_run_with_private_chat_aliases(tmp_path):
    import shutil
    import uuid
    from tests.test_shared_workflow_worker import code_graph, terminal
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
    from vibecanvas_api.services.sandbox.manager import WorkflowRunBinding
    from vibecanvas_api.services.sandbox.workflow_rpc_pool import WorkflowRpcPool
    if not shutil.which('bwrap'):
        pytest.skip('requires bubblewrap')
    shared = tmp_path / 'shared'
    (shared / 'chats').mkdir(parents=True)
    (shared / 'chats' / 'owner-secret').write_text('private')
    for actor in ('alice', 'bob'):
        private = tmp_path / actor
        session = SandboxSession(
            tenant_id=actor, wf_id=f'{actor}-workspace', user_id=actor,
            run_dir=str(private), runtime_dir=None, overlay_dir=None,
            provider=BubblewrapProvider(shutil.which('bwrap')), base_binds=[], expose_run=True,
            workflow_run_binding=WorkflowRunBinding('owner', 'workflow', str(shared)),
        )
        for folder in session.workspace_folders:
            (private / folder).mkdir(parents=True)
        (private / 'chats' / actor).write_text(actor)
        graph = code_graph()
        graph['node_2']['node_config']['process_fn'] = '''def process_fn(inputs):
    from pathlib import Path
    name = inputs['label']
    assert sorted(p.name for p in Path('/chats').iterdir()) == [name]
    assert sorted(p.name for p in Path('/run/chats').iterdir()) == [name]
    if name == 'bob':
        assert Path('/run/result.txt').read_text() == 'alice'
    Path('/run/result.txt').write_text(name)
    return {'label': name}
'''
        pool = WorkflowRpcPool.for_session(session=session, revision=actor, workflow=graph, capacity=1)
        execution = str(uuid.uuid4())
        try:
            async with pool.acquire(execution) as slot:
                await slot.invoke(execution, {'label': actor, 'delay': 0}, {})
                state = await terminal(slot, execution)
                assert state['status'] == 'succeeded', state
                await slot.release(execution, state['seq'])
        finally:
            await pool.close()
        assert (shared / 'result.txt').read_text() == actor


def test_shared_run_artifacts_exclude_private_workspace_roots(tmp_path):
    from vibecanvas_api.services.workflow_artifacts import _files
    (tmp_path / 'chats').mkdir()
    (tmp_path / 'chats' / 'secret').write_text('private')
    (tmp_path / 'result.txt').write_text('shared')
    assert list(_files(str(tmp_path), frozenset({'chats'}))) == [('result.txt', b'shared')]
