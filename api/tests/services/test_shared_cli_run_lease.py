"""Shared source admission must not move the caller's execution lease."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.types import Action
from vibecanvas_api.services.agent_runtime import cli_run_lease
from vibecanvas_api.services.workflow_deletion import check_dependencies


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [True, False])
async def test_shared_lease_admission_and_private_ownership(monkeypatch, allowed):
    ctx = SimpleNamespace(tenant_id='caller', username='user', turn_id='turn')
    state = {'tenant': 'caller', 'locked': False, 'inserted': False}
    session = AsyncMock()

    async def execute(statement, params):
        sql = str(statement)
        if 'set_config' in sql:
            assert state['locked']
            state['tenant'] = params['tenant']
        else:
            assert 'INSERT INTO workflow_cli_leases' in sql
            assert state['tenant'] == params['tenant'] == 'caller'
            assert params['wf'] == 'shared-source'
            state['inserted'] = True

    session.execute.side_effect = execute

    @asynccontextmanager
    async def scope(**kwargs):
        yield session

    async def admit(actual_session, actual_ctx, workflow_id, action):
        assert actual_ctx is ctx and action == Action.EXECUTE
        if not allowed:
            raise ToolError('permission_denied', 'Denied')
        state['tenant'] = 'owner'

    async def lock(actual_session, workflow_id):
        assert state['tenant'] == 'owner'
        state['locked'] = True

    monkeypatch.setattr(cli_run_lease, 'session_scope', scope)
    monkeypatch.setattr(cli_run_lease, '_require_active_chat_write', AsyncMock())
    monkeypatch.setattr(cli_run_lease, '_workflow_decision', admit)
    monkeypatch.setattr(cli_run_lease, 'lock_live_workflow', lock)
    if allowed:
        await cli_run_lease.reserve(ctx, 'run', 'shared-source')
        assert state['inserted']
    else:
        with pytest.raises(ToolError):
            await cli_run_lease.reserve(ctx, 'run', 'shared-source')
        assert not state['locked'] and not state['inserted']


@pytest.mark.asyncio
async def test_foreign_cli_lease_blocks_source_deletion(monkeypatch):
    session = AsyncMock()
    session.execute.return_value = Mock(first=Mock(return_value=None))
    session.scalar.return_value = 'owner'
    connection = AsyncMock()

    async def execute(statement, params):
        assert params == {'id': 'source', 'owner': 'owner'}
        return Mock(first=Mock(return_value=(1,) if 'workflow_cli_leases' in str(statement) else None))

    connection.execute.side_effect = execute

    @asynccontextmanager
    async def admin():
        yield connection

    monkeypatch.setattr('vibecanvas_api.storage.sync_session.short_admin_connection', admin)
    with pytest.raises(ToolError, match='workflow_in_use'):
        await check_dependencies(session, 'source')
