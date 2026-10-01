import asyncio
from inspect import signature

import pytest

from vibecanvas_api.services.deployment_completion import complete_before_cancelling


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_repeated_http_cancellation_waits_for_worker_and_final_record(fails):
    entered, finish = asyncio.Event(), asyncio.Event()
    states = []
    @complete_before_cancelling
    async def handler(value: int):
        states.append('running')
        entered.set()
        await finish.wait()
        states.append('failed' if fails else 'succeeded')
        if fails:
            raise RuntimeError('worker failed')
        return value
    assert signature(handler).parameters['value'].annotation is int
    task = asyncio.create_task(handler(1))
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert states == ['running']
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert states[-1] == ('failed' if fails else 'succeeded')
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_normal_completion_preserves_result_and_error():
    @complete_before_cancelling
    async def handler(value):
        if value < 0:
            raise ValueError('invalid')
        return value
    assert await handler(7) == 7
    with pytest.raises(ValueError, match='invalid'):
        await handler(-1)


def test_decorated_routes_keep_fastapi_dependency_schema():
    from fastapi import FastAPI
    from vibecanvas_api.routes import deployment_invoke, deployments
    app = FastAPI()
    app.include_router(deployment_invoke.router)
    app.include_router(deployments.router)
    paths = app.openapi()['paths']
    public = paths['/api/v1/deployments/{slug}/invoke']['post']
    assert any(p['name'] == 'authorization' and p['in'] == 'header' for p in public['parameters'])
    assert 'requestBody' in public
    assert 'requestBody' in paths['/api/v1/deployments/{dep_id}/test-invoke']['post']


@pytest.mark.asyncio
async def test_shared_dashboard_invocation_records_owner_tenant_despite_progress_disconnect(pg_engine, app_engine, monkeypatch):
    import uuid
    from unittest.mock import AsyncMock
    from fastapi import Request
    from sqlalchemy import text
    from tests.test_deployment_rollout import setup_rollout
    from vibecanvas_api.auth.deps import AuthContext
    from vibecanvas_api.routes import deployments
    from vibecanvas_api.storage.db import short_session_scope
    _, dep, _ = await setup_rollout(pg_engine, app_engine)
    # Permission admission is independent of this regression: model an already
    # authorized share whose request DB session was rebound to the owner.
    monkeypatch.setattr(deployments, '_authorize_deployment', AsyncMock())
    from tests.test_deployment_test_invoke import _complete_dispatch
    runner = AsyncMock(side_effect=_complete_dispatch)
    monkeypatch.setattr('vibecanvas_api.services.deployment_dispatch.dispatch_invocation', runner)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [],
        'state': {'cli_deployment_progress': AsyncMock(side_effect=RuntimeError('observer disconnected'))}})
    ctx = AuthContext(user_id=str(dep['user_id']), tenant_id=str(uuid.uuid4()), email='test@example.com')
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as session:
        result = await deployments.test_invoke(dep_id=dep['id'], body={}, request=request,
            ctx=ctx, session=session, service=object())
    assert runner.call_args.kwargs['tenant_id'] == str(dep['tenant_id'])
    assert result['status'] == 'succeeded'
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as session:
        row = (await session.execute(text('SELECT tenant_id, status FROM deployment_invocations WHERE id=:id'),
            {'id': uuid.UUID(result['execution_id'])})).one()
    assert row == (dep['tenant_id'], 'succeeded')
