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


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["releasing", "release_failed"])
async def test_unconfirmed_sandbox_release_preserves_all_durable_files(monkeypatch, lifecycle):
    from types import SimpleNamespace
    session = AsyncMock()
    session.execute.side_effect = [Mock(scalar_one_or_none=Mock(return_value="tenant")),
                                   Mock(one=Mock(return_value=SimpleNamespace(completed_at=None)))]
    session.get.return_value = SimpleNamespace(deleted_at=object())
    @asynccontextmanager
    async def admin_session():
        yield session
    manager = SimpleNamespace(close_session=AsyncMock(return_value={"lifecycle_state": lifecycle}))
    store = Mock()
    monkeypatch.setattr("vibecanvas_api.storage.sync_session.short_admin_session", admin_session)
    monkeypatch.setattr("vibecanvas_api.services.sandbox.manager.get_sandbox_manager", lambda: manager)
    monkeypatch.setattr("vibecanvas_api.services.object_store.get_object_store", lambda: store)
    with pytest.raises(RuntimeError, match="shutdown/persistence is not confirmed"):
        await cleanup("workflow")
    assert session.execute.await_count == 2
    session.delete.assert_not_awaited()
    store.delete_bytes.assert_not_called()
