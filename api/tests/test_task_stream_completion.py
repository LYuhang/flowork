"""Already-consumed terminal rows close streams without dropping replay."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from vibecanvas_api.routes import tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'state', 'cursor', 'event', 'expected'), [
    ('batch_exec', 'finished', 42, 'terminal', 204),
    ('batch_exec', 'failed', 42, 'terminal', 204),
    ('batch_exec', 'cancelled', 42, 'terminal', 204),
    ('batch_exec', 'finished', 41, 'terminal', 200),
    ('batch_exec', 'finished', 42, 'progress', 200),
    ('batch_exec', 'running', 42, 'terminal', 200),
    ('scheduled_run', 'paused', 42, 'terminal', 200),
])
async def test_completed_cursor(monkeypatch, kind, state, cursor, event, expected):
    identifier, tenant = uuid4(), uuid4()
    authorize = AsyncMock()
    monkeypatch.setattr(tasks, '_authorize_task', authorize)
    repo = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(task_type=kind, status=state, tenant_id=tenant)),
        events_for_task=AsyncMock(return_value=[SimpleNamespace(id=42, event_type=event)]),
    )
    monkeypatch.setattr(tasks, 'TasksRepo', lambda session: repo)
    response = await tasks.stream_task_events(identifier,
        SimpleNamespace(headers={'Last-Event-ID': str(cursor)}),
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    assert response.status_code == expected
    authorize.assert_awaited_once()
    if expected == 204:
        assert response.body == b''
    else:
        await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_completed_stream_still_requires_authorization(monkeypatch):
    monkeypatch.setattr(tasks, '_authorize_task', AsyncMock(side_effect=HTTPException(404)))
    with pytest.raises(HTTPException) as error:
        await tasks.stream_task_events(uuid4(), SimpleNamespace(), SimpleNamespace(),
                                       SimpleNamespace(), SimpleNamespace())
    assert error.value.status_code == 404
