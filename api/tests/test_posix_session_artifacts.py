from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vibecanvas_api.services.sandbox import session_executions as module


@pytest.mark.asyncio
async def test_persistent_run_does_not_snapshot_or_restore_files(monkeypatch):
    session = SimpleNamespace(tenant_id='tenant', persistent_run_binding=object(), workflow_run_source=None,
                              _sync_mount_folder=AsyncMock())
    executions = module.SessionExecutions(session)
    @asynccontextmanager
    async def acquire(execution_id):
        yield SimpleNamespace(artifacts='/persistent/run')
    @asynccontextmanager
    async def database(**kwargs):
        yield object()
    async def run_driver(self, *, inputs, context):
        assert '_workflow_resume_from' not in context
        await self.persist()
        return {'outputs': {'result': 7}}
    class Driver:
        def __init__(self, **kwargs):
            self.persist = kwargs['persist_artifacts']
        run = run_driver
    persist = AsyncMock(side_effect=AssertionError('no snapshot'))
    restore = AsyncMock(side_effect=AssertionError('no restore'))
    monkeypatch.setattr(module, 'persist_workflow_artifacts', persist)
    monkeypatch.setattr(module, 'restore_workflow_artifacts', restore)
    monkeypatch.setattr(module, 'short_session_scope', database)
    monkeypatch.setattr(module, 'WorkflowExecutionDriver', Driver)
    monkeypatch.setattr(module.WorkflowHistoryRepo, 'get', AsyncMock(return_value={'status': 'succeeded'}))
    result = await executions._execute(SimpleNamespace(pool=SimpleNamespace(acquire=acquire)),
                                       'execution', {}, {'_workflow_resume_from': 'old'}, 'workflow')
    assert result['result']['outputs']['result'] == 7
    persist.assert_not_called()
    restore.assert_not_called()
    session._sync_mount_folder.assert_not_called()
