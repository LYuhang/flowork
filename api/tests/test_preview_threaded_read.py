"""Slow preview I/O must leave the request event loop available."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from vibecanvas_api.routes import previews


@pytest.mark.asyncio
async def test_slow_descriptor_does_not_block_other_coroutines(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    def descriptor(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(2), 'request loop could not release the slow reader'
        return 'descriptor'
    monkeypatch.setattr(previews, '_authorize_file_ref', AsyncMock())
    monkeypatch.setattr(previews, '_resolve_file', AsyncMock(return_value=object()))
    monkeypatch.setattr(previews, '_resolve_descriptor', descriptor)
    task = asyncio.create_task(previews.resolve_preview(
        SimpleNamespace(file_ref=object()), request=object(), auth=object(), session=object(), service=object()))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        assert not task.done()
    finally:
        release.set()
    assert await task == 'descriptor'


@pytest.mark.asyncio
async def test_read_revision_conflict_survives_thread_boundary(monkeypatch):
    monkeypatch.setattr(previews, '_authorize_file_ref', AsyncMock())
    monkeypatch.setattr(previews, '_resolve_file', AsyncMock(return_value=object()))
    def conflict(*args):
        raise HTTPException(409, 'preview_revision_conflict')
    monkeypatch.setattr(previews, '_resolve_descriptor', conflict)
    with pytest.raises(HTTPException) as error:
        await previews.resolve_preview(SimpleNamespace(file_ref=object()), request=object(),
                                       auth=object(), session=object(), service=object())
    assert error.value.status_code == 409
