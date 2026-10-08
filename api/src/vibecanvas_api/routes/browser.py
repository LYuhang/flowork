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
from vibecanvas_api.browser.connection_errors import (
    BrowserInitializationError,
    EXTENSION_DISCONNECTED,
    INITIALIZATION_TIMEOUT,
    SESSION_CHANGED,
    initialization_reason,
)
from vibecanvas_api.browser.session_control import (
    BrowserSessionControlError,
    BrowserSessionLease,
    confirm_sidepanel_browser_session,
)
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.services.agent_resources.capability import (
    verify_agent_capability,
)

router = APIRouter(prefix="/api/v1/browser", tags=["browser"])
log = logging.getLogger(__name__)
PLAYWRIGHT_INITIALIZATION_TIMEOUT_SECONDS = 10.0
PLAYWRIGHT_AUTHORIZATION_INTERVAL_SECONDS = 5.0


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
    """Check live identity, Chat permission, originating turn and workspace.

    The raw CDP endpoint must not depend on the CLI caller doing this check:
    an old signed capability is not permission to borrow a later turn's lease.
    """
    from vibecanvas_api.services.agent_resources.context import resolve_context

    try:
        await resolve_context(capability)
        return True
    except PermissionError:
        return False
    except Exception as exc:
        # Authorization failures, including dependency outages, fail closed.
        # Exception text can contain credentials or SQL parameters.
        log.warning("browser_cdp_authorization_unavailable error_type=%s", type(exc).__name__)
        return False


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
        await ws.send_text(encode("echo", id="browser_auth", channel="system", transport=transport_id,
                                  data={"type": "auth_status", "expires_at": scoped.exp}))
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
            if msg["kind"] == "auth_refresh":
                # Refreshing the panel mints a new short-lived token. Verify it
                # on the existing socket so live commands/captures survive UI
                # reloads. A different account/session must open a new socket.
                data = msg.get("data")
                token = data.get("token") if isinstance(data, dict) else None
                fresh = verify_scoped_token(token, secret) if isinstance(token, str) else None
                identity = ("user_id", "tenant_id", "wf_id", "browser_id", "extension_id",
                            "session_id", "session_generation", "session_audience", "audience")
                allowed = bool(fresh and all(getattr(fresh, key) == getattr(scoped, key) for key in identity)
                               and fresh.exp >= scoped.exp and await _browser_session_is_live(fresh))
                if allowed:
                    scoped = fresh
                await ws.send_text(encode("echo", id=msg["id"], channel="system", transport=transport_id,
                                          data={"type": "auth_refresh", "ok": allowed, "expires_at": scoped.exp}))
            elif msg["kind"] == "ping":
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
        # Transport teardown has no authority over another browser's lease.
        # The CDP controller owns the exact Chat and generation to reconcile.
        registry.unregister(transport_id, _send)


@router.websocket("/playwright/cdp")
async def playwright_cdp(ws: WebSocket):
    """Authenticated standard-CDP endpoint used by official Playwright.

    The endpoint is not a public browser debug port. A turn-scoped Platform MCP
    capability identifies the user and Chat; the live database browser lease
    and connected extension are rechecked before the WebSocket is accepted.
    Raw CDP frames are wrapped only while crossing the Flowork extension WSS.
    """

    capability = verify_agent_capability(
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
    pending_receive: asyncio.Task | None = None
    download_requests: dict[int, str] = {}
    registered_download_sets: set[str] = set()

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
                BrowserInitializationError(initialization_reason(error.get("message")))
            )
            return
        if "id" not in message and "method" not in message:
            return
        # The request declares no filenames or candidate authority. Register
        # only the extension's correlated response on this authenticated relay.
        download_method = download_requests.pop(message.get("id"), None)
        if download_method and isinstance(result, dict) and not error:
            from vibecanvas_api.services.agent_runtime.browser_download_choices import DownloadScope, register_candidates
            try:
                async with session_scope(tenant_id=capability.tenant_id) as session:
                    if download_method == "Flowork.downloadInfo" and result.get("status") in {"ready", "selection_required"}:
                        choice_set_id = await register_candidates(session, capability,
                            DownloadScope(transport_id, browser_session_id, browser_session_generation),
                            tab_id=result.get("tab_id"), capture_id=result.get("capture_id"), candidates=result.get("candidates"))
                        registered_download_sets.add(choice_set_id)
                        message = {**message, "result": {**result, "choice_set_id": choice_set_id}}
                    elif download_method == "Flowork.downloadEnd":
                        from sqlalchemy import text
                        await session.execute(text("""UPDATE browser_download_choices SET revoked_at=now()
                            WHERE run_id=:run AND capture_id=:capture AND revoked_at IS NULL"""),
                            {"run": capability.turn_id, "capture": result.get("capture_id")})
            except Exception as exc:
                # Validation/SQL exceptions can contain private file metadata.
                log.warning("browser_download_registration_failed chat_id=%s error_type=%s", capability.chat_id, type(exc).__name__)
                message = {"id": message["id"], "sessionId": message.get("sessionId"), "error": {
                    "code": -32603, "message": "Download registration failed or the browser session changed. No file was transferred. Do not trigger the download again."}}
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
        await asyncio.wait_for(initialized, timeout=PLAYWRIGHT_INITIALIZATION_TIMEOUT_SECONDS)
        await confirm_sidepanel_browser_session(
            BrowserSessionLease(
                tenant_id=capability.organization_id,
                user_id=capability.user_id,
                chat_id=capability.chat_id,
                browser_session_id=browser_session_id,
                session_generation=browser_session_generation,
            )
        )
        renew_at = loop.time()  # Recheck changes that raced initialization.
        while True:
            remaining = capability.expires_at - time.time()
            if remaining <= 0:
                await ws.close(code=4401)
                return
            if loop.time() >= renew_at:
                if not await _platform_session_is_live(capability):
                    await ws.close(code=4401)
                    return
                current_transport = registry.find_for_session(
                    capability.organization_id, capability.user_id, capability.session_id,
                )
                async with session_scope(tenant_id=capability.organization_id) as session:
                    current_binding = await ChatRepo(session, capability.user_id).get_browser_binding(capability.chat_id)
                if (current_transport != transport_id or not current_binding
                        or current_binding.get("status") not in {"attaching", "attached"}
                        or current_binding.get("browser_session_id") != browser_session_id
                        or int(current_binding.get("browser_session_generation") or 0) != browser_session_generation):
                    await ws.close(code=4409, reason=SESSION_CHANGED)
                    return
                renew_at = loop.time() + PLAYWRIGHT_AUTHORIZATION_INTERVAL_SECONDS
            if pending_receive is None:
                pending_receive = asyncio.create_task(ws.receive_json())
            # Keep the same receive across authorization ticks. Idle and busy
            # sockets both renew; queued commands wait behind the fresh check.
            done, _ = await asyncio.wait({pending_receive}, timeout=min(remaining, max(0, renew_at - loop.time())))
            if not done:
                continue
            if time.time() >= capability.expires_at or loop.time() >= renew_at:
                continue
            message = pending_receive.result()
            pending_receive = None
            if not isinstance(message, dict):
                await ws.close(code=4400)
                return
            if message.get("method") == "Flowork.downloadRead":
                from vibecanvas_api.services.agent_runtime.browser_transfer_calls import authorize_native_read
                try:
                    params = message.get("params") or {}
                    async with session_scope(tenant_id=capability.tenant_id) as session:
                        grant = await authorize_native_read(session, capability,
                            transfer_id=str(params.get("transfer_id") or ""), capture_id=str(params.get("capture_id") or ""))
                    # This action cannot be produced by a raw CDP method. The
                    # extension receives host-validated ownership, not an Agent
                    # assertion of consent or a local filesystem path.
                    await _send_extension("download_read", request=message, grant=grant)
                except Exception:
                    await ws.send_json({"id": message.get("id"), "sessionId": message.get("sessionId"), "error": {
                        "code": -32603, "message": "download_approval_required: The live command, approved file or browser ownership could not be verified. No file bytes were read."}})
                continue
            if message.get("method") in {"Flowork.downloadInfo", "Flowork.downloadEnd"}:
                request_id = message.get("id")
                if type(request_id) is not int or request_id in download_requests:
                    await ws.close(code=4400)
                    return
                download_requests[request_id] = message["method"]
            await _send_extension("request", request=message)
    except BrowserInitializationError as error:
        await ws.close(code=1011, reason=str(error))
    except asyncio.TimeoutError:
        await ws.close(code=1011, reason=INITIALIZATION_TIMEOUT)
    except BrowserSessionControlError:
        await ws.close(code=4409, reason=SESSION_CHANGED)
    except WebSocketDisconnect:
        if ws.client_state.name == "CONNECTED" and ws.application_state.name == "CONNECTED":
            await ws.close(code=1011, reason=EXTENSION_DISCONNECTED)
    finally:
        if pending_receive is not None:
            pending_receive.cancel()
            await asyncio.gather(pending_receive, return_exceptions=True)
        download_requests.clear()
        if registered_download_sets:
            try:
                from sqlalchemy import text
                async with session_scope(tenant_id=capability.tenant_id) as session:
                    await session.execute(text("""UPDATE browser_download_choices SET revoked_at=now()
                        WHERE choice_set_id=ANY(:ids) AND run_id=:run AND revoked_at IS NULL"""),
                        {"ids": list(registered_download_sets), "run": capability.turn_id})
            except Exception:
                log.warning("browser_download_scope_cleanup_failed chat_id=%s", capability.chat_id)
        if not initialized.done():
            initialized.cancel()
        elif not initialized.cancelled():
            # A transport failure may race the acknowledgement/refusal before
            # the initialization await begins. Retrieve any stored exception.
            initialized.exception()
        owns_controller = playwright_controllers.unregister(
            transport_id=transport_id,
            channel=channel,
            sender=_send_to_playwright,
        )
        # A replacement controller can connect before this one's finally runs.
        # Its session fence may be unchanged during recovery; do not close it.
        if owns_controller:
            if not registry.is_connected(transport_id):
                try:
                    async with session_scope(tenant_id=capability.organization_id) as session:
                        await ChatRepo(session, capability.user_id).mark_browser_lost(
                            chat_id=capability.chat_id,
                            browser_session_id=browser_session_id,
                            browser_session_generation=browser_session_generation,
                            reason="websocket_disconnected",
                        )
                except Exception:
                    log.warning("browser_disconnect_state_update_failed chat_id=%s", capability.chat_id)
            try:
                await _send_extension("close")
            except Exception:
                pass
