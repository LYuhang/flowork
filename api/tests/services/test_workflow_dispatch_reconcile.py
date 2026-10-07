"""A dispatch failure is reported, never retried or replaced by a new run."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.sandbox import workflow_execution_driver as module
from vibecanvas_api.services.sandbox.workflow_rpc import WorkflowRpcError


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ConnectionError('disconnected'), TimeoutError('dispatch timed out'),
                                  WorkflowRpcError('runtime_error')])
async def test_dispatch_error_is_propagated_without_retry(monkeypatch, error):
    @asynccontextmanager
    async def session():
        yield None

    repo = SimpleNamespace(bind_runtime=AsyncMock())
    monkeypatch.setattr(module, 'WorkflowHistoryRepo', lambda _: repo)
    client = SimpleNamespace(generation='original', call=AsyncMock())
    slot = SimpleNamespace(alive=True, client=client, process_identity=lambda: {},
                           invoke=AsyncMock(side_effect=error))
    owner = module.WorkflowExecutionDriver(tenant_id='tenant', execution_id='same-id',
                                           slot=slot, persist_artifacts=AsyncMock())
    monkeypatch.setattr(owner, '_session', session)
    with pytest.raises(type(error)) as raised:
        await owner._run(inputs={}, context={}, changed=asyncio.Event())
    assert raised.value is error
    slot.invoke.assert_awaited_once_with('same-id', {}, {}, require_approval_resume=True)
    client.call.assert_not_awaited()
    repo.bind_runtime.assert_awaited_once_with('same-id', 'original', process={})
