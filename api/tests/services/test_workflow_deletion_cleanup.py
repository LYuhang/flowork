"""Exercise lazy production imports even when a cleanup job is a no-op."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from vibecanvas_api.services.workflow_deletion import cleanup


@pytest.mark.asyncio
async def test_cleanup_without_ledger_is_safe_and_importable(monkeypatch):
    session = AsyncMock()
    session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=None))

    @asynccontextmanager
    async def admin_session():
        yield session

    monkeypatch.setattr(
        "vibecanvas_api.storage.sync_session.short_admin_session", admin_session
    )
    await cleanup("missing-cleanup-fixture")
    session.execute.assert_awaited_once()
