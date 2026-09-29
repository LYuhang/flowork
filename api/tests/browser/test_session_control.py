from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.browser import session_control as control


@pytest.fixture
def repo(monkeypatch):
    repo = AsyncMock()

    @asynccontextmanager
    async def scope(**kwargs):
        assert kwargs == {"tenant_id": "tenant"}
        yield object()

    monkeypatch.setattr(control, "session_scope", scope)
    monkeypatch.setattr(control, "ChatRepo", lambda session, user: repo)
    return repo


@pytest.mark.asyncio
async def test_conflicting_chat_denial_is_actionable_and_does_not_release_owner(repo):
    repo.reserve_browser_session.return_value = {
        "ok": False, "error_code": "browser_busy", "conflicting_chat_id": "old-chat",
    }
    with pytest.raises(control.BrowserSessionControlError) as error:
        await control.reserve_sidepanel_browser_session(tenant_id="tenant", user_id="user", chat_id="new-chat")
    assert error.value.code == "browser_busy"
    assert "Creating a new Chat does not transfer browser permission" in str(error.value)
    assert "No browser command was executed" in str(error.value)
    assert "do not retry automatically" in str(error.value)
    assert repo.reserve_browser_session.await_count == 1
    repo.release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_reservation_error_does_not_claim_another_chat_owns_browser(repo):
    repo.reserve_browser_session.return_value = {"ok": False, "error_code": "chat_not_found"}
    with pytest.raises(control.BrowserSessionControlError) as error:
        await control.reserve_sidepanel_browser_session(tenant_id="tenant", user_id="user", chat_id="missing-chat")
    assert error.value.code == "chat_not_found"
    assert "Another Chat" not in str(error.value)
    repo.release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_chat_reuses_exact_persisted_generation(repo):
    repo.reserve_browser_session.return_value = {"ok": True, "already_attached": True, "binding": {
        "browser_session_id": "brs_existing", "browser_session_generation": 7,
    }}
    lease = await control.reserve_sidepanel_browser_session(tenant_id="tenant", user_id="user", chat_id="old-chat")
    assert lease == control.BrowserSessionLease("tenant", "user", "old-chat", "brs_existing", 7)
    repo.release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_finished_turn_releases_its_attached_lease_with_exact_fence(repo):
    repo.release_browser_session.return_value = {"ok": True}
    lease = control.BrowserSessionLease("tenant", "user", "old-chat", "session", 7)
    assert await control.release_sidepanel_browser_session(lease)
    repo.release_browser_session.assert_awaited_once_with(
        "old-chat", browser_session_id="session", browser_session_generation=7,
        reason="browser_turn_finished",
    )


@pytest.mark.asyncio
async def test_finished_old_turn_cannot_release_a_replacement_generation(repo):
    repo.release_browser_session.return_value = {"ok": False, "error_code": "browser_session_generation_mismatch"}
    lease = control.BrowserSessionLease("tenant", "user", "old-chat", "old-session", 7)
    assert not await control.release_sidepanel_browser_session(lease)
    assert repo.release_browser_session.await_count == 1
