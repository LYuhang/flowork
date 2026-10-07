from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.browser import session_control
from vibecanvas_api.services.agent_runtime import browser_cli_authorization as auth


@pytest.fixture
def authority(monkeypatch):
    capability = SimpleNamespace(organization_id='tenant', user_id='user',
                                 session_id='extension-session', chat_id='new-chat')
    monkeypatch.setattr(auth, 'validate', lambda *args: None)
    monkeypatch.setattr(auth, 'verify_agent_capability', lambda *args, **kwargs: capability)
    resolve = AsyncMock()
    monkeypatch.setattr(auth, 'resolve_context', resolve)
    monkeypatch.setattr(auth.registry, 'find_for_session', lambda *args: 'transport')
    reserve = AsyncMock(return_value=session_control.BrowserSessionLease(
        'tenant', 'user', 'new-chat', 'lease', 3))
    monkeypatch.setattr(session_control, 'reserve_sidepanel_browser_session', reserve)
    return reserve, resolve


@pytest.mark.asyncio
async def test_browser_command_acquires_lease_after_fresh_authorization(authority):
    reserve, resolve = authority
    result = await auth.authorize_browser_cli(operation='browser.tab-list', arguments={},
                                             token='private', endpoint='private-endpoint')
    resolve.assert_awaited_once()
    reserve.assert_awaited_once_with(tenant_id='tenant', user_id='user', chat_id='new-chat')
    assert result['fence'] == ['transport', 'lease', 3]


@pytest.mark.asyncio
async def test_busy_is_tool_error_and_does_not_release_other_chat(authority, monkeypatch):
    reserve, _ = authority
    reserve.side_effect = session_control.BrowserSessionControlError('browser_busy', 'Another Chat controls this browser')
    release = AsyncMock()
    monkeypatch.setattr(session_control, 'release_sidepanel_browser_session', release)
    result = await auth.authorize_browser_cli(operation='browser.tab-list', arguments={},
                                             token='private', endpoint='private-endpoint')
    assert result['error'] == 'browser_busy'
    assert 'Another Chat' in result['message']
    assert 'bearer' not in result
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_identity_cannot_reserve_browser(authority):
    reserve, resolve = authority
    resolve.side_effect = PermissionError('revoked')
    with pytest.raises(PermissionError, match='revoked'):
        await auth.authorize_browser_cli(operation='browser.tab-list', arguments={},
                                         token='private', endpoint='private-endpoint')
    reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnected_extension_does_not_reserve_browser(authority, monkeypatch):
    reserve, _ = authority
    monkeypatch.setattr(auth.registry, 'find_for_session', lambda *args: None)
    result = await auth.authorize_browser_cli(operation='browser.tab-list', arguments={},
                                             token='private', endpoint='private-endpoint')
    assert result['error'] == 'browser_disconnected'
    reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_renewal_after_detach_does_not_reacquire_control(authority, monkeypatch):
    from contextlib import asynccontextmanager
    reserve, _ = authority
    @asynccontextmanager
    async def scope(**kwargs):
        yield object()
    monkeypatch.setattr(auth, 'session_scope', scope)
    monkeypatch.setattr(auth, 'ChatRepo', lambda *args: SimpleNamespace(
        get_browser_binding=AsyncMock(return_value={'status': 'inactive', 'browser_session_id': None, 'browser_session_generation': 3})))
    result = await auth.authorize_browser_cli(operation='browser.tab-list', arguments={},
        token='private', endpoint='private-endpoint', expected_fence=['transport', 'lease', 3])
    assert result['error'] == 'browser_control_released'
    reserve.assert_not_awaited()
