import json
import subprocess
import sys
import pytest

from vibecanvas_api.services.sandbox.local_activity import active_executions, execution_activity


def test_finished_runs_leave_scan_directory_but_preserve_exit_evidence(tmp_path):
    from uuid import uuid4
    from vibecanvas_api.services.sandbox.local_activity import execution_alive
    run_id = uuid4().hex
    assert execution_alive(run_id, tmp_path) is None
    with execution_activity(tmp_path, run_id=run_id):
        assert execution_alive(run_id, tmp_path) is True
        assert active_executions(tmp_path) == 1
    assert execution_alive(run_id, tmp_path) is False
    assert active_executions(tmp_path) == 0
    assert list((tmp_path / 'local-executions').iterdir()) == []
    assert execution_alive(run_id, tmp_path) is False
    assert active_executions(tmp_path) == 0
    with pytest.raises(FileExistsError):
        with execution_activity(tmp_path, run_id=run_id):
            pytest.fail('a completed ID must not acquire a new lifetime')


def test_activity_tracks_each_process_lifetime(tmp_path):
    assert active_executions(tmp_path) == 0
    with execution_activity(tmp_path):
        assert active_executions(tmp_path) == 1
        with execution_activity(tmp_path):
            assert active_executions(tmp_path) == 2
        assert active_executions(tmp_path) == 1
    assert active_executions(tmp_path) == 0


def test_killed_process_does_not_leave_false_busy_marker(tmp_path):
    child = subprocess.Popen([sys.executable, '-c',
        "from vibecanvas_api.services.sandbox.local_activity import execution_activity; "
        "import sys,time; "
        "ctx=execution_activity(sys.argv[1]); ctx.__enter__(); "
        "print('ready',flush=True); time.sleep(30)", str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        assert active_executions(tmp_path) == 1
        child.kill()
        child.wait(timeout=5)
        assert active_executions(tmp_path) == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdout.close()


async def test_cli_owner_identity_comes_from_live_runtime_handle():
    import asyncio
    from types import SimpleNamespace
    from vibecanvas_api.services.sandbox.manager import SandboxSession
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                               start_new_session=True)
    session = SimpleNamespace(_lock=asyncio.Lock(), _runtime_handle=SimpleNamespace(proc=process))
    try:
        record = await SandboxSession.local_execution_process(session)
        assert record['pid'] == record['group'] == process.pid
        assert record['host_id'] and record['boot_id'] and record['start'] >= 0
        process.kill()
        process.wait(timeout=5)
        with pytest.raises(RuntimeError, match='execution_lost'):
            await SandboxSession.local_execution_process(session)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_supervisor_publishes_local_activity_for_idle_sweep(tmp_path):
    from vibecanvas_api.sandbox_entry import _ActivityPublisher
    publisher = _ActivityPublisher(str(tmp_path))
    with execution_activity(tmp_path):
        publisher.refresh_local_executions()
        state = json.loads((tmp_path / 'activity.json').read_text())
        assert state['active_jobs'] == 1
        assert state['idle_since_monotonic_ns'] is None
        sequence = state['sequence']
    publisher.refresh_local_executions()
    state = json.loads((tmp_path / 'activity.json').read_text())
    assert state['active_jobs'] == 0 and state['sequence'] > sequence
    assert state['idle_since_monotonic_ns'] is not None


def test_only_published_unlocked_run_has_finished_evidence(tmp_path):
    from uuid import uuid4
    run_id = uuid4().hex
    with execution_activity(tmp_path, run_id=run_id):
        assert active_executions(tmp_path) == 1
        assert not (tmp_path/'local-execution-finished'/run_id).exists()
        # A process still acquiring its lock has no public run marker.
        (tmp_path/'local-executions'/('.'+uuid4().hex)).touch()
        assert active_executions(tmp_path) == 1
        assert list((tmp_path/'local-execution-finished').iterdir()) == []
    assert active_executions(tmp_path) == 0
    assert (tmp_path/'local-execution-finished'/run_id).is_file()


async def test_manager_requires_positive_exit_evidence(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from uuid import uuid4
    from vibecanvas_api.services.sandbox.manager import SandboxManager
    run_id = uuid4().hex
    session = SimpleNamespace(closed=False, _fileop_pool=SimpleNamespace(work_root=str(tmp_path)))
    manager = SimpleNamespace(_lock=asyncio.Lock(), _sessions={('tenant', 'sandbox'): session}, _closed_local_executions=set())
    assert await SandboxManager.local_execution_exited(manager, 'tenant', 'sandbox', run_id) is None
    with execution_activity(tmp_path, run_id=run_id):
        active_executions(tmp_path)
        assert await SandboxManager.local_execution_exited(manager, 'tenant', 'sandbox', run_id) is None
    active_executions(tmp_path)
    assert await SandboxManager.local_execution_exited(manager, 'tenant', 'sandbox', run_id) is True
    assert await SandboxManager.local_execution_exited(manager, 'other', 'sandbox', run_id) is None
    session.closed = True
    assert await SandboxManager.local_execution_exited(manager, 'tenant', 'sandbox', run_id) is None


async def test_sandbox_close_records_exit_only_after_success(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4
    from vibecanvas_api.services.sandbox.manager import SandboxManager
    run_id = uuid4().hex
    finished_id = uuid4().hex
    with execution_activity(tmp_path, run_id=finished_id):
        pass
    assert active_executions(tmp_path) == 0
    with execution_activity(tmp_path, run_id=run_id):
        session = SimpleNamespace(tenant_id='tenant', wf_id='sandbox',
            _fileop_pool=SimpleNamespace(work_root=str(tmp_path)), close=AsyncMock(side_effect=RuntimeError('close failed')))
        manager = SimpleNamespace(_closed_local_executions=set(), _failed_closes={})
        await SandboxManager._close_session_best_effort(manager, session, reason='test')
        assert manager._closed_local_executions == set()
        session.close = AsyncMock()
        await SandboxManager._close_session_best_effort(manager, session, reason='test')
        assert ('tenant', 'sandbox', run_id) in manager._closed_local_executions
        assert ('tenant', 'sandbox', finished_id) in manager._closed_local_executions
