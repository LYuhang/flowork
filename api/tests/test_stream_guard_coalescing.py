"""Coalesce concurrent stream checks without retaining authorization results."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.authorization import stream_guard as guard
from vibecanvas_api.authorization.types import Action, ResourceRef, ResourceType


def arguments():
    return dict(auth=AuthContext(user_id='user', tenant_id='org', email='test@example.com',
        session_id='session', membership_id='membership'), openfga_client=object(),
        resource=ResourceRef(ResourceType.CHAT, 'chat', 'org'), action=Action.VIEW)


@pytest.mark.asyncio
async def test_shared_read_is_removed_before_next_permission_check(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(**kwargs):
        entered.set()
        await release.wait()
        return True
    mock = AsyncMock(side_effect=read)
    monkeypatch.setattr(guard, '_read_authorization_lease', mock)
    args = arguments()
    tasks = [asyncio.create_task(guard.authorization_lease_is_valid(**args)) for _ in range(5)]
    await entered.wait()
    await asyncio.sleep(0)
    assert mock.call_count == 1
    release.set()
    assert await asyncio.gather(*tasks) == [True] * 5
    assert not guard._pending_checks
    mock.side_effect = None
    mock.return_value = False
    assert not await guard.authorization_lease_is_valid(**args)
    assert mock.call_count == 2
    assert not guard._pending_checks


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['user', 'session', 'audience', 'privileged', 'generation', 'organization', 'membership', 'resource', 'action', 'client'])
async def test_distinct_authorization_boundaries_do_not_join(monkeypatch, change):
    release = asyncio.Event()
    async def read(**kwargs):
        await release.wait()
        return True
    mock = AsyncMock(side_effect=read)
    monkeypatch.setattr(guard, '_read_authorization_lease', mock)
    a = arguments()
    b = dict(a)
    if change == 'user': b['auth'] = replace(a['auth'], user_id='other')
    elif change == 'session': b['auth'] = replace(a['auth'], session_id='other')
    elif change == 'audience': b['auth'] = replace(a['auth'], session_audience='support')
    elif change == 'privileged': b['auth'] = replace(a['auth'], privileged_resource_id='other')
    elif change == 'generation': b['auth'] = replace(a['auth'], session_generation=2)
    elif change == 'organization': b['auth'] = replace(a['auth'], active_organization_id='other')
    elif change == 'membership': b['auth'] = replace(a['auth'], membership_id='other')
    elif change == 'resource': b['resource'] = replace(a['resource'], id='other')
    elif change == 'action': b['action'] = Action.UPDATE
    else: b['openfga_client'] = object()
    tasks = [asyncio.create_task(guard.authorization_lease_is_valid(**args)) for args in (a, b)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert mock.call_count == 2
    release.set()
    assert await asyncio.gather(*tasks) == [True, True]
    assert not guard._pending_checks


@pytest.mark.asyncio
async def test_cancelled_observer_does_not_cancel_other_observer(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(**kwargs):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(guard, '_read_authorization_lease', read)
    args = arguments()
    a, b = [asyncio.create_task(guard.authorization_lease_is_valid(**args)) for _ in range(2)]
    await entered.wait()
    a.cancel()
    with pytest.raises(asyncio.CancelledError): await a
    release.set()
    assert await b
    assert not guard._pending_checks


@pytest.mark.asyncio
async def test_last_observer_cancels_and_cleans_up_read(monkeypatch):
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def read(**kwargs):
        entered.set()
        try: await asyncio.Event().wait()
        finally: stopped.set()
    monkeypatch.setattr(guard, '_read_authorization_lease', read)
    task = asyncio.create_task(guard.authorization_lease_is_valid(**arguments()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert stopped.is_set()
    assert not guard._pending_checks
