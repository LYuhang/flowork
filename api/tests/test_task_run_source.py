from uuid import uuid4

import pytest
from pydantic import ValidationError
from vibecanvas_api.services.sandbox.contracts import TaskRunSource


def test_task_run_source_has_only_stable_task_identity():
    task_id = uuid4()
    source = TaskRunSource(task_id=task_id)
    assert TaskRunSource.model_validate_json(source.model_dump_json()) == source
    assert source.model_dump(mode='json') == {'task_id': str(task_id)}
    with pytest.raises(ValidationError):
        TaskRunSource(task_id=task_id, directory='/etc')
    with pytest.raises(ValidationError):
        TaskRunSource(task_id=task_id, tenant_id='other')
    with pytest.raises(ValidationError):
        TaskRunSource(task_id='../escape')


def test_task_binding_survives_protobuf_roundtrip():
    from vibecanvas_api.services.sandbox.proto import sandbox_service_pb2 as pb
    task_id = str(uuid4())
    request = pb.AcquireRequest(
        scope=pb.SandboxScope(tenant_id='tenant', scope_id='lease-specific'),
        task_run_source=pb.TaskRunSource(task_id=task_id),
    )
    decoded = pb.AcquireRequest.FromString(request.SerializeToString())
    assert decoded.scope.scope_id == 'lease-specific'
    assert decoded.task_run_source.task_id == task_id
    assert not decoded.HasField('workflow_run_source')


def test_shared_task_run_keeps_job_staging_private(tmp_path, monkeypatch):
    from vibecanvas_api.services.sandbox.manager import SandboxSession
    from vibecanvas_api.config import config
    monkeypatch.setattr(config, 'workspace_storage_backend', 'posix')
    session = SandboxSession.__new__(SandboxSession)
    session.task_run_source = TaskRunSource(task_id=uuid4())
    session.workflow_run_dir = str(tmp_path / 'persistent' / 'task')
    session.pool_runs_root = str(tmp_path / 'private-worker' / 'runs')
    session.persistent_run_binding = object()
    assert session._workflow_staging_root() == session.pool_runs_root


def test_rpc_worker_receives_shared_run_without_other_resource_roots(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from vibecanvas_api.services.sandbox import workflow_rpc_pool as module
    from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity
    task_id = str(uuid4())
    binding = PosixWorkspaceStorage(str(tmp_path)).acquire(WorkspaceIdentity('tenant', 'task', task_id))
    captured = []
    def worker(**kwargs):
        captured.append(kwargs)
        return object()
    monkeypatch.setattr(module, 'WorkflowRpcWorker', worker)
    session = SimpleNamespace(workspace_folders=(), _rw_binds=[], workflow_run_source=None,
                              persistent_run_binding=binding, skills_dir=None, provider=object())
    pool = module.WorkflowRpcPool.for_session(session=session, revision='test', workflow={}, capacity=2)
    try:
        pool.factory(0)
        pool.factory(1)
        assert {w['artifacts_root'] for w in captured} == {binding.directory}
        assert captured[0]['root'] != captured[1]['root']
        assert all(not w['rw_binds'] for w in captured)
    finally:
        pool._workspace.cleanup()


@pytest.mark.asyncio
async def test_acquire_rejects_different_task_on_existing_scope():
    from types import SimpleNamespace
    from vibecanvas_api.services.sandbox.manager import SandboxManager
    manager = SandboxManager(max_resident=2, idle_ttl_s=600)
    original = TaskRunSource(task_id=uuid4())
    manager._sessions[('tenant', 'worker-scope')] = SimpleNamespace(
        closed=False, user_id='user', workflow_run_source=None, task_run_source=original)
    with pytest.raises(RuntimeError, match='workspace_identity_mismatch'):
        await manager.get_session('tenant', 'worker-scope', user_id='user',
                                  task_run_source=TaskRunSource(task_id=uuid4()))
