from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from vibecanvas_api.routes import tasks
from vibecanvas_api.services import state_notifications


@pytest.mark.asyncio
async def test_activity_is_authorized_and_does_not_end_at_task_completion(monkeypatch):
    identifier = uuid4()
    authorize = AsyncMock()
    monkeypatch.setattr(tasks, '_authorize_task', authorize)
    get = AsyncMock(return_value=SimpleNamespace(status='succeeded'))
    monkeypatch.setattr(tasks, 'TasksRepo', lambda session: SimpleNamespace(get=get))
    seen = []
    async def changes(channel, resource_id, authorized):
        seen.append((channel, resource_id))
        yield True
        yield False
        yield True  # A later evaluation after inference completed.
    monkeypatch.setattr(state_notifications, 'invalidations', changes)
    session = SimpleNamespace(commit=AsyncMock())
    response = await tasks.task_activity(identifier, SimpleNamespace(), SimpleNamespace(), session, SimpleNamespace())
    frames = [frame async for frame in response.body_iterator]
    assert frames == ['event: changed\ndata: {}\n\n', ': heartbeat\n\n', 'event: changed\ndata: {}\n\n']
    assert seen == [('flowork_task_activity', str(identifier))]
    authorize.assert_awaited_once()
    session.commit.assert_awaited_once()
    get.assert_awaited_once()


@pytest.mark.asyncio
async def test_activity_denies_before_opening_stream(monkeypatch):
    monkeypatch.setattr(tasks, '_authorize_task', AsyncMock(side_effect=HTTPException(403)))
    with pytest.raises(HTTPException) as exc:
        await tasks.task_activity(uuid4(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    assert exc.value.status_code == 403
