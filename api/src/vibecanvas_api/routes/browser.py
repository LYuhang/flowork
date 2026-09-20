"""Browser-automation transport hub.

Two surfaces:
  POST /api/v1/browser/token  — auth'd mint of a short-lived scoped token (§15.A)
  WS   /api/v1/browser/ws     — the tenant-scoped Playwright relay (one per browser)

The WebSocket carries lifecycle events and authenticated Playwright CDP relay
frames. The retired Flowork command/observation protocol is intentionally not
accepted here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from vibecanvas_api.auth.deps import AuthContext, current_user
from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.config import config
from vibecanvas_api.browser.scoped_token import (
    MAX_BROWSER_TOKEN_TTL_S,
    ScopedAuth,
    mint_scoped_token,
    verify_scoped_token,
)
from vibecanvas_api.browser.ws_auth import (
    BROWSER_WS_PROTOCOL,
    parse_browser_ws_protocols,
)
from vibecanvas_api.browser.envelope import encode, decode
from vibecanvas_api.browser.registry import registry
from vibecanvas_api.browser.playwright_registry import playwright_controllers
from vibecanvas_api.browser.session_control import (
    BrowserSessionLease,
    confirm_sidepanel_browser_session,
)
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.services.platform_mcp.capability import (
    verify_platform_mcp_capability,
)

router = APIRouter(prefix="/api/v1/browser", tags=["browser"])
log = logging.getLogger(__name__)


def _chat_id_from_channel(channel: str | None) -> str:
    return channel[len("chat:") :] if channel and channel.startswith("chat:") else ""


async def _handle_browser_event(msg: dict, scoped) -> dict | None:
    data = msg.get("data") or {}
    if not isinstance(data, dict):
        return None
    event_type = str(data.get("type") or "")
    if event_type not in {"browser_session_changed", "browser_session_snapshot"}:
        return None
    chat_id = str(
        data.get("chat_id") or _chat_id_from_channel(msg.get("channel")) or ""
    )
    if not chat_id:
        return None
    status = str(data.get("status") or "")
    browser_session_id = str(
        data.get("browser_session_id") or data.get("session_id") or ""
    )
    reason = str(data.get("reason") or status or "browser_session_changed")
    try:
        event_seq = int(data.get("event_seq") or 0)
    except Exception:
        event_seq = 0
    try:
        session_generation = int(data.get("session_generation") or 0)
    except Exception:
        session_generation = 0

    async with session_scope(tenant_id=scoped.tenant_id) as session:
        repo = ChatRepo(session, scoped.user_id)
        if event_type == "browser_session_snapshot":
            if not browser_session_id or session_generation <= 0:
                return None
            result = await repo.reconcile_browser_session_snapshot(
                chat_id=chat_id,
                browser_session_id=browser_session_id,
                browser_session_generation=session_generation,
                event_seq=event_seq,
                controlled=bool(data.get("controlled")),
                reason=reason or "reconnect_snapshot",
            )
        elif status in {"released", "inactive"}:
            if not browser_session_id or session_generation <= 0:
                return None
            result = await repo.release_browser_session(
                chat_id,
                browser_session_id=browser_session_id,
                browser_session_generation=session_generation,
                event_seq=event_seq,
                reason=reason,
            )
        elif status == "lost":
            if browser_session_id and session_generation > 0:
                result = await repo.mark_browser_lost(
                    chat_id=chat_id,
                    browser_session_id=browser_session_id,
                    browser_session_generation=session_generation,
                    event_seq=event_seq,
                    reason=reason,
                )
            else:
                return None
        else:
            # ``attached`` is confirmed by the authenticated Playwright CDP
            # initialization. Only snapshot/lost/terminal lifecycle events
            # mutate durable state through this WebSocket path.
            return None
    if not result.get("ok"):
        return None
    return {
        "type": "browser_session_event_ack",
        "browser_session_id": browser_session_id,
        "session_generation": session_generation,
        "event_seq": event_seq,
    }


class MintIn(BaseModel):
    wf_id: str = Field(min_length=1, max_length=512)
    browser_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._~-]+$",
    )


@router.post("/token")
async def mint_token(body: MintIn, auth: AuthContext = Depends(current_user)):
    if config.environment == "production" and auth.session_audience != "extension":
        raise HTTPException(status_code=403, detail="extension_session_required")
    secret = config.browser_token_secret
    try:
        token = mint_scoped_token(
            auth.user_id,
            auth.tenant_id,
            body.wf_id,
            secret,
            browser_id=body.browser_id,
            extension_id=config.browser_extension_id,
            session_id=auth.session_id,
            session_generation=auth.session_generation,
            session_audience=auth.session_audience,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_browser_binding") from exc
    return {"token": token, "expires_in": MAX_BROWSER_TOKEN_TTL_S}


def _extension_origin_is_valid(origin: str | None, extension_id: str) -> bool:
    return (origin or "").rstrip("/") == f"chrome-extension://{extension_id}"


async def _browser_session_is_live(scoped: ScopedAuth) -> bool:
    """Bind the stateless capability back to revocable Session state."""
    try:
        session_id = uuid.UUID(scoped.session_id)
        user_id = uuid.UUID(scoped.user_id)
    except ValueError:
        return False
    async with session_scope() as session:
        repo = AuthRepo(session)
        row = await repo.get_session_by_id(session_id, user_id=user_id)
        if row is None or row.expires_at <= datetime.now(timezone.utc):
            return False
        if (
            str(row.active_organization_id) != scoped.tenant_id
            or int(row.generation) != scoped.session_generation
            or str(row.audience) != scoped.session_audience
        ):
            return False
        membership = await repo.get_membership(
            user_id=row.user_id,
            organization_id=row.active_organization_id,
        )
        return membership is not None and membership.status == "active"


async def _platform_session_is_live(capability) -> bool:
    """Revalidate the auth Session carried by a Platform MCP capability."""
    try:
        session_id = uuid.UUID(capability.session_id)
        user_id = uuid.UUID(capability.user_id)
    except (TypeError, ValueError):
        return False
    async with session_scope() as session:
        repo = AuthRepo(session)
        row = await repo.get_session_by_id(session_id, user_id=user_id)
        if row is None or row.expires_at <= datetime.now(timezone.utc):
            return False
        if (
            str(row.active_organization_id) != capability.organization_id
            or int(row.generation) != capability.session_generation
        ):
            return False
        membership = await repo.get_membership(
            user_id=row.user_id,
            organization_id=row.active_organization_id,
        )
        return membership is not None and membership.status == "active"


def _bearer_token(ws: WebSocket) -> str:
    authorization = str(ws.headers.get("authorization") or "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


@router.websocket("/ws")
async def ws_hub(
    ws: WebSocket,
):
    handshake = parse_browser_ws_protocols(ws.headers.get("sec-websocket-protocol"))
    if handshake is None:
        await ws.close(code=4401)  # auth failure (pre-accept close)
        return
    secret = config.browser_token_secret
    scoped = verify_scoped_token(handshake.token, secret)
    if scoped is None:
        await ws.close(code=4401)  # auth failure (pre-accept close)
        return
    if (
        scoped.browser_id != handshake.browser_id
        or scoped.extension_id != config.browser_extension_id
        or not _extension_origin_is_valid(
            ws.headers.get("origin"),
            scoped.extension_id,
        )
        or not await _browser_session_is_live(scoped)
    ):
        await ws.close(code=4401)
        return
    transport_id = f"{scoped.tenant_id}:{scoped.user_id}:{handshake.browser_id}"
    # Select only the public protocol version. Never echo the credential-bearing
    # offered protocol in the handshake response.
    await ws.accept(subprotocol=BROWSER_WS_PROTOCOL)

    async def _send(raw: str) -> None:
        await ws.send_text(raw)

    registry.register(
        transport_id,
        _send,
        session_id=scoped.session_id,
    )
    try:
        while True:
            remaining = scoped.exp - time.time()
            if remaining <= 0:
                await ws.close(code=4401)
                return
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=remaining)
            except asyncio.TimeoutError:
                await ws.close(code=4401)
                return
            try:
                msg = decode(raw)
            except ValueError:
                continue
            if msg["kind"] == "ping":
                await ws.send_text(
                    encode(
                        "echo",
                        id=msg["id"],
                        channel=msg["channel"],
                        transport=transport_id,
                        data=msg.get("data"),
                    )
                )
            elif msg["kind"] == "event":
                ack = await _handle_browser_event(msg, scoped)
                if ack is not None:
                    await ws.send_text(
                        encode(
                            "echo",
                            id=msg["id"],
                            channel=msg["channel"],
                            transport=transport_id,
                            data=ack,
                        )
                    )
            elif msg["kind"] == "playwright_relay":
                data = msg.get("data") or {}
                message = data.get("message") if isinstance(data, dict) else None
                if isinstance(message, dict):
                    await playwright_controllers.forward_extension_message(
                        transport_id=transport_id,
                        channel=str(msg.get("channel") or ""),
                        message=message,
                    )
    except WebSocketDisconnect:
        pass
    finally:
        removed_current_connection = registry.unregister(transport_id, _send)
        if removed_current_connection:
            # The live channel is ephemeral, but its loss is durable control
            # state. A later reconnect snapshot can restore the same fenced
            # session; until then tools must tell the Agent to verify first.
            try:
                async with session_scope(tenant_id=scoped.tenant_id) as session:
                    repo = ChatRepo(session, scoped.user_id)
                    binding = await repo.get_active_browser_binding_for_user()
                    if (
                        binding
                        and binding.get("status") in {"attaching", "attached"}
                        and binding.get("browser_session_id")
                    ):
                        await repo.mark_browser_lost(
                            chat_id=str(binding["chat_id"]),
                            browser_session_id=str(binding["browser_session_id"]),
                            browser_session_generation=int(
                                binding.get("browser_session_generation") or 0
                            ),
                            reason="websocket_disconnected",
                        )
            except Exception:
                # Connection teardown must always finish; the next tool call's
                # missing transport remains a second, fail-closed signal.
                pass


@router.websocket("/playwright/cdp")
async def playwright_cdp(ws: WebSocket):
    """Authenticated standard-CDP endpoint used by official Playwright.

    The endpoint is not a public browser debug port. A turn-scoped Platform MCP
    capability identifies the user and Chat; the live database browser lease
    and connected extension are rechecked before the WebSocket is accepted.
    Raw CDP frames are wrapped only while crossing the Flowork extension WSS.
    """

    capability = verify_platform_mcp_capability(
        _bearer_token(ws),
        secret=config.signing_secret,
        server="browser",
    )
    if capability is None:
        log.warning(
            "browser_playwright_cdp_rejected "
            "reason=capability_invalid authorization_present=%s",
            bool(_bearer_token(ws)),
        )
        await ws.close(code=4401)
        return
    if not await _platform_session_is_live(capability):
        log.warning(
            "browser_playwright_cdp_rejected "
            "reason=session_invalid chat_id=%s session_generation=%s",
            capability.chat_id,
            capability.session_generation,
        )
        await ws.close(code=4401)
        return

    channel = f"chat:{capability.chat_id}"
    transport_id = registry.find_for_session(
        capability.organization_id,
        capability.user_id,
        capability.session_id,
    )
    if transport_id is None:
        log.warning(
            "browser_playwright_cdp_rejected "
            "reason=extension_transport_missing chat_id=%s",
            capability.chat_id,
        )
        await ws.close(code=4409)
        return
    async with session_scope(tenant_id=capability.organization_id) as session:
        binding = await ChatRepo(session, capability.user_id).get_browser_binding(
            capability.chat_id
        )
    if (
        not binding
        or binding.get("status") not in {"attaching", "attached"}
        or not binding.get("browser_session_id")
        or int(binding.get("browser_session_generation") or 0) <= 0
    ):
        log.warning(
            "browser_playwright_cdp_rejected "
            "reason=browser_lease_missing chat_id=%s browser_status=%s",
            capability.chat_id,
            str((binding or {}).get("status") or ""),
        )
        await ws.close(code=4409)
        return

    browser_session_id = str(binding["browser_session_id"])
    browser_session_generation = int(binding["browser_session_generation"])
    await ws.accept()
    loop = asyncio.get_running_loop()
    initialized: asyncio.Future[None] = loop.create_future()

    async def _send_to_playwright(message: dict) -> None:
        # Initialization acknowledgements belong to the bridge control plane,
        # not CDP. Standard CDP responses have an id and events have a method.
        result = message.get("result")
        if isinstance(result, dict) and result.get("initialized") is True:
            if not initialized.done():
                initialized.set_result(None)
            return
        error = message.get("error")
        if isinstance(error, dict) and not initialized.done():
            initialized.set_exception(
                RuntimeError(str(error.get("message") or "Playwright bridge failed"))
            )
            return
        if "id" not in message and "method" not in message:
            return
        await ws.send_json(message)

    playwright_controllers.register(
        transport_id=transport_id,
        channel=channel,
        send=_send_to_playwright,
    )

    async def _send_extension(action: str, **data) -> None:
        raw = encode(
            "playwright_relay",
            id=f"pw_{uuid.uuid4().hex}",
            channel=channel,
            transport=transport_id,
            producer="playwright",
            data={
                "action": action,
                "browser_session_id": browser_session_id,
                "session_generation": browser_session_generation,
                **data,
            },
        )
        if not await registry.send_to(transport_id, raw):
            raise WebSocketDisconnect(code=1011)

    try:
        await _send_extension("initialize")
        # Do not consume queued Playwright commands until the extension has
        # released the legacy debugger owner and constructed the CDP bridge.
        await asyncio.wait_for(initialized, timeout=10.0)
        await confirm_sidepanel_browser_session(
            BrowserSessionLease(
                tenant_id=capability.organization_id,
                user_id=capability.user_id,
                chat_id=capability.chat_id,
                browser_session_id=browser_session_id,
                session_generation=browser_session_generation,
            )
        )
        while True:
            remaining = capability.exp - time.time()
            if remaining <= 0:
                await ws.close(code=4401)
                return
            try:
                message = await asyncio.wait_for(
                    ws.receive_json(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                await ws.close(code=4401)
                return
            if not isinstance(message, dict):
                await ws.close(code=4400)
                return
            await _send_extension("request", request=message)
    except WebSocketDisconnect:
        pass
    finally:
        playwright_controllers.unregister(
            transport_id=transport_id,
            channel=channel,
            sender=_send_to_playwright,
        )
        try:
            await _send_extension("close")
        except Exception:
            pass
