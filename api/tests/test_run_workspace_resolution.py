from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from vibecanvas_api.services.run_workspace_resolution import resolve_run_workspace, run_relative_path
from vibecanvas_api.services.workspace_storage import WorkspaceIdentity
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['task', 'deployment'])
async def test_executions_resolve_to_same_resource_directory(monkeypatch, kind):
    resource_id, tenant = str(uuid4()), str(uuid4())
    get = AsyncMock(return_value={'source_type': kind, 'source_id': resource_id})
    monkeypatch.setattr(WorkflowHistoryRepo, 'get', get)
    session = Mock(execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=tenant))))
    first = await resolve_run_workspace(session, str(uuid4()))
    second = await resolve_run_workspace(session, str(uuid4()))
    assert first == second == WorkspaceIdentity(tenant, kind, resource_id)
    assert get.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['task', 'deployment'])
async def test_stable_run_reference_uses_resource_registry(kind):
    resource_id, tenant = str(uuid4()), str(uuid4())
    session = Mock(execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=tenant))))
    assert await resolve_run_workspace(session, f'{kind}-run-{resource_id}') == WorkspaceIdentity(tenant, kind, resource_id)
    session.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(FileNotFoundError):
        await resolve_run_workspace(session, f'{kind}-run-{resource_id}')


def test_run_path_cannot_expose_private_workflow_chats():
    identity = WorkspaceIdentity('tenant', 'workflow', 'workflow')
    assert run_relative_path(identity, '/run/result.json') == 'result.json'
    for path in ['/run/chats/private/file', '/run/../chats/file', '/data/file', '/run/']:
        with pytest.raises(ValueError):
            run_relative_path(identity, path)
