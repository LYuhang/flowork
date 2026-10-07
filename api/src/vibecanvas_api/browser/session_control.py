"""Durable browser-session control plane for Playwright-backed Chats.

This module owns the database lease transitions required before the sandbox
Browser CLI runtime may connect to the authenticated CDP endpoint. It intentionally
does not expose Agent tools and does not send the legacy DOM command protocol.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from vibecanvas_api.config import config
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope


class BrowserSessionControlError(RuntimeError):
    """Fail-closed browser lease error with a stable product code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass(frozen=True)
class BrowserSessionLease:
    tenant_id: str
    user_id: str
    chat_id: str
    browser_session_id: str
    session_generation: int


async def reserve_sidepanel_browser_session(
    *,
    tenant_id: str,
    user_id: str,
    chat_id: str,
) -> BrowserSessionLease:
    """Reserve or reuse this Chat's sole browser lease before CLI startup."""

    requested_session_id = f"brs_{uuid.uuid4().hex}"
    async with session_scope(tenant_id=str(tenant_id)) as session:
        result = await ChatRepo(session, str(user_id)).reserve_browser_session(
            chat_id=str(chat_id),
            browser_session_id=requested_session_id,
            lost_grace_seconds=config.browser_lost_grace_seconds,
        )
    if not result.get("ok"):
        code = str(result.get("error_code") or "browser_session_conflict")
        raise BrowserSessionControlError(
            code,
            (
                "Another Chat already controls this browser. Continue in that Chat, "
                "or ask the user to release its browser control before this Chat uses browser tools. "
                "Creating a new Chat does not transfer browser permission. "
                "No browser command was executed; do not retry automatically."
                if code == "browser_busy" else
                "The browser-control lease could not be reserved. "
                "No browser command was executed. Check the extension connection and retry only after resolving the reported error."
            ),
        )
    binding = result.get("binding")
    if not isinstance(binding, dict):
        raise BrowserSessionControlError(
            "browser_session_persist_failed",
            "The durable browser lease returned no binding state.",
        )
    browser_session_id = str(
        binding.get("browser_session_id") or requested_session_id
    )
    generation = int(binding.get("browser_session_generation") or 0)
    if not browser_session_id or generation <= 0:
        raise BrowserSessionControlError(
            "browser_session_persist_failed",
            "The durable browser lease is missing its session fence.",
        )
    return BrowserSessionLease(
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        chat_id=str(chat_id),
        browser_session_id=browser_session_id,
        session_generation=generation,
    )


async def confirm_sidepanel_browser_session(lease: BrowserSessionLease) -> None:
    """Confirm attach only after the extension initialized Playwright CDP."""

    async with session_scope(tenant_id=lease.tenant_id) as session:
        result = await ChatRepo(session, lease.user_id).confirm_browser_session_attached(
            chat_id=lease.chat_id,
            browser_session_id=lease.browser_session_id,
            browser_session_generation=lease.session_generation,
        )
    if not result.get("ok"):
        code = str(result.get("error_code") or "browser_session_state_mismatch")
        raise BrowserSessionControlError(
            code,
            "The extension initialized, but the durable browser lease could not be confirmed.",
        )


async def release_unconfirmed_browser_session(
    lease: BrowserSessionLease,
    *,
    reason: str,
) -> bool:
    """Release only this generation when MCP failed before attach confirmation."""

    async with session_scope(tenant_id=lease.tenant_id) as session:
        repo = ChatRepo(session, lease.user_id)
        binding = await repo.get_browser_binding(lease.chat_id)
        if (
            not binding
            or binding.get("status") != "attaching"
            or str(binding.get("browser_session_id") or "")
            != lease.browser_session_id
            or int(binding.get("browser_session_generation") or 0)
            != lease.session_generation
        ):
            return False
        result = await repo.release_browser_session(
            lease.chat_id,
            browser_session_id=lease.browser_session_id,
            browser_session_generation=lease.session_generation,
            reason=reason,
        )
    return bool(result.get("ok"))


async def release_sidepanel_browser_session(lease: BrowserSessionLease) -> bool:
    """End exactly this turn's lease after all browser runtime work is closed."""
    async with session_scope(tenant_id=lease.tenant_id) as session:
        result = await ChatRepo(session, lease.user_id).release_browser_session(
            lease.chat_id,
            browser_session_id=lease.browser_session_id,
            browser_session_generation=lease.session_generation,
            reason="browser_turn_finished",
        )
    return bool(result.get("ok"))


async def release_chat_browser_session(*, tenant_id: str, user_id: str, chat_id: str) -> bool:
    """Release a lazily acquired lease after the Chat's exclusive turn exits."""
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        binding = await ChatRepo(session, user_id).get_browser_binding(chat_id)
    if not binding or not binding.get("browser_session_id"):
        return False
    return await release_sidepanel_browser_session(BrowserSessionLease(
        tenant_id=tenant_id, user_id=user_id, chat_id=chat_id,
        browser_session_id=str(binding["browser_session_id"]),
        session_generation=int(binding["browser_session_generation"]),
    ))


__all__ = [
    "BrowserSessionControlError",
    "BrowserSessionLease",
    "confirm_sidepanel_browser_session",
    "release_unconfirmed_browser_session",
    "release_sidepanel_browser_session",
    "release_chat_browser_session",
    "reserve_sidepanel_browser_session",
]
