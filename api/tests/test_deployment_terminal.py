"""PTY transport admission, bounded output, and connection cleanup."""
import asyncio
import base64
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocket

from vibecanvas_api.routes import deployment_terminal as route


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', [None, 'https://untrusted.invalid'])
async def test_terminal_rejects_origin_before_authentication(monkeypatch, origin):
    auth = AsyncMock()
    monkeypatch.setattr(route, 'current_user', auth)
    headers = [(b'host', b'testserver')]
    if origin:
        headers.append((b'origin', origin.encode()))
    ws = WebSocket({'type': 'websocket', 'scheme': 'ws', 'path': '/',
        'query_string': b'', 'headers': headers, 'server': ('testserver', 80)}, AsyncMock(), AsyncMock())
    with pytest.raises(HTTPException) as error:
        await route.terminal_identity(ws, uuid.uuid4())
    assert error.value.status_code == 403
    auth.assert_not_awaited()


class Socket:
    def __init__(self):
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.send_json = AsyncMock()
        self.send_bytes = AsyncMock()
        self.messages = asyncio.Queue()
    async def receive_text(self):
        return await self.messages.get()


@pytest.mark.asyncio
async def test_denied_terminal_never_opens_shell(monkeypatch):
    ws = Socket()
    manager = SimpleNamespace(deployment_terminal=AsyncMock())
    monkeypatch.setattr(route, 'get_sandbox_manager', lambda: manager)
    monkeypatch.setattr(route, 'terminal_identity', AsyncMock(side_effect=HTTPException(403)))
    await route.deployment_terminal(ws, uuid.uuid4())
    ws.accept.assert_not_awaited()
    manager.deployment_terminal.assert_not_awaited()
    ws.close.assert_awaited_once_with(code=1008)


@pytest.mark.asyncio
async def test_output_waits_for_renderer_and_disconnect_releases_shell(monkeypatch):
    ws = Socket()
    target = dict(tenant_id='tenant', deployment_id='dep', revision_id='revision', user_id='user')
    monkeypatch.setattr(route, 'terminal_identity', AsyncMock(return_value=(target, ('binding',))))
    reads = 0
    window_full = asyncio.Event()
    async def rpc(**kwargs):
        nonlocal reads
        if kwargs['action'] == 'read':
            reads += 1
            if reads == 2:
                window_full.set()
            return {'ok': True, 'data': base64.b64encode(b'x' * 32768).decode(), 'exit_code': None}
        return {'ok': True}
    manager = SimpleNamespace(deployment_terminal=AsyncMock(side_effect=rpc))
    monkeypatch.setattr(route, 'get_sandbox_manager', lambda: manager)
    task = asyncio.create_task(route.deployment_terminal(ws, uuid.uuid4()))
    try:
        await asyncio.wait_for(window_full.wait(), 2)
        await asyncio.sleep(.25)
        assert reads == 2, 'unrendered output must be bounded'
        await ws.messages.put('{"type":"ack","bytes":32768}')
        for _ in range(20):
            if reads == 3:
                break
            await asyncio.sleep(.02)
        assert reads == 3
        await ws.messages.put('{"type":"disconnect"}')
        await asyncio.wait_for(task, 2)
        assert manager.deployment_terminal.call_args.kwargs['action'] == 'close'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_instance_switch_closes_old_terminal(monkeypatch):
    ws = Socket()
    target = dict(tenant_id='tenant', deployment_id='dep', revision_id='old', user_id='user')
    identity = AsyncMock(side_effect=[(target, ('binding',)), ({**target, 'revision_id': 'new'}, ('binding',))])
    monkeypatch.setattr(route, 'terminal_identity', identity)
    # Replace only this module's clock; do not alter asyncio's global clock.
    clock = iter([0, 6])
    monkeypatch.setattr(route, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    manager = SimpleNamespace(deployment_terminal=AsyncMock(return_value={'ok': True}))
    monkeypatch.setattr(route, 'get_sandbox_manager', lambda: manager)
    await route.deployment_terminal(ws, uuid.uuid4())
    ws.send_json.assert_any_await({'type': 'instance_changed'})
    assert manager.deployment_terminal.call_args.kwargs['action'] == 'close'
    assert manager.deployment_terminal.call_args.kwargs['revision_id'] == 'old'


@pytest.mark.asyncio
async def test_identity_uses_owner_tenant_and_requires_update(monkeypatch):
    from vibecanvas_api.auth.deps import AuthContext
    owner, user, deployment, revision = (uuid.uuid4() for _ in range(4))
    ctx = AuthContext(user_id=str(user), tenant_id=str(uuid.uuid4()), email='test@example.com')
    monkeypatch.setattr(route, 'validate_browser_origin', lambda request: None)
    auth = AsyncMock(return_value=ctx)
    monkeypatch.setattr(route, 'current_user', auth)
    async def tenant_db(request, auth):
        yield object()
    monkeypatch.setattr(route, 'tenant_db', tenant_db)
    monkeypatch.setattr(route, 'get_authz_service', AsyncMock(return_value=object()))
    authorize = AsyncMock()
    monkeypatch.setattr(route, '_authorize_deployment', authorize)
    repository = SimpleNamespace(get=AsyncMock(return_value={
        'tenant_id': owner, 'enabled': True, 'active_revision_id': revision}))
    monkeypatch.setattr(route, 'DeploymentsRepo', lambda session: repository)
    ws = SimpleNamespace(scope={'type': 'websocket', 'headers': [], 'path_params': {'dep_id': deployment}})
    target, _ = await route.terminal_identity(ws, deployment)
    assert target['tenant_id'] == str(owner)
    assert target['revision_id'] == str(revision)
    assert authorize.call_args.kwargs['action'] == route.Action.UPDATE
    assert authorize.call_args.kwargs['consistency'] == route.ConsistencyPreference.HIGHER_CONSISTENCY
    assert auth.call_args.kwargs['creds'] is None
    # A failed authorization must not query/open the deployment shell.
    authorize.side_effect = HTTPException(404)
    repository.get.reset_mock()
    with pytest.raises(HTTPException):
        await route.terminal_identity(ws, deployment)
    repository.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_retirement_before_periodic_check_reports_instance_switch(monkeypatch):
    ws = Socket()
    target = dict(tenant_id='tenant', deployment_id='dep', revision_id='old', user_id='user')
    monkeypatch.setattr(route, 'terminal_identity', AsyncMock(side_effect=[
        (target, ('binding',)), ({**target, 'revision_id': 'new'}, ('binding',))]))
    async def rpc(**kwargs):
        return {'ok': kwargs['action'] != 'read'}
    manager = SimpleNamespace(deployment_terminal=AsyncMock(side_effect=rpc))
    monkeypatch.setattr(route, 'get_sandbox_manager', lambda: manager)
    await asyncio.wait_for(route.deployment_terminal(ws, uuid.uuid4()), 2)
    ws.send_json.assert_any_await({'type': 'instance_changed'})
    assert manager.deployment_terminal.call_args.kwargs['action'] == 'close'
