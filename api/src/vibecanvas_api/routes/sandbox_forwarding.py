"""Authenticated loopback previews through isolated path or domain gateways.

The API never connects to a user-selected host. Requests traverse sandboxd and
are executed inside the existing, network-isolated Agent runtime.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import time
import uuid
from contextlib import aclosing, suppress
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select

from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import ChatProject

from .service_preview_paths import redirect_url, rewrite_document

gateway = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@gateway.exception_handler(HTTPException)
async def preview_error(request: Request, exc: HTTPException):
    title = "Service preview unavailable"
    explanation = (
        "Reopen the preview from the conversation to renew access."
        if exc.status_code == 401
        else "Check that the sandbox and its web service are still running."
    )
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><title>{title}</title>'
        f"<h2>{title}</h2><p>{explanation}</p><pre>{html.escape(str(exc.detail))}</pre>",
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


COOKIE = "__Host-flowork-preview"
TTL = 3600
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def preview_domain() -> str:
    value = os.environ.get("FLOWORK_SERVICE_PREVIEW_DOMAIN", "").strip().lower()
    if value and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise ValueError("invalid FLOWORK_SERVICE_PREVIEW_DOMAIN")
    platform_host = urlsplit(config.public_urls.public_url).hostname
    if value and platform_host and (value == platform_host or platform_host.endswith("." + value)):
        raise ValueError("preview domain must be separate from the platform host")
    return value


def local_service_url(value: str):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Expected an HTTP(S) localhost, 127.0.0.1 or [::1] URL")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("invalid service port")
    return parsed, port


def codec():
    key = hashlib.sha256(("flowork-service-preview-v1:" + config.signing_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def hostname(token: str) -> str:
    claims = json.loads(codec().decrypt(token.encode(), ttl=TTL))
    identity = f"{claims['tenant']}:{claims['scope']}:{claims['runtime_id']}:{claims.get('loopback_host', '127.0.0.1')}:{claims.get('scheme', 'http')}"
    sandbox_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"p{claims['port']}-s{sandbox_id}.{preview_domain()}"


def claims_for(token: str, host: str) -> dict:
    try:
        claims = json.loads(codec().decrypt(token.encode(), ttl=TTL))
        if host != hostname(token) or claims["exp"] <= time.time():
            raise ValueError("expired or incorrect origin")
        return claims
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise HTTPException(401, "service_preview_expired_reopen_from_chat") from None


def issue_forwarding_target(
    *,
    tenant: str,
    user: str,
    project: str,
    scope: str,
    runtime_id: str,
    port: int,
    scheme: str,
    loopback_host: str,
    path: str,
    browser_origin: str | None = None,
) -> dict:
    """Issue a port-specific URL for an already-authorized sandbox generation."""
    claims = dict(
        tenant=tenant,
        user=user,
        project=project,
        scope=scope,
        runtime_id=runtime_id,
        port=port,
        scheme=scheme,
        loopback_host=loopback_host,
        path=path,
        browser_origin=browser_origin or config.public_urls.public_url.rstrip("/"),
        exp=int(time.time()) + TTL,
    )
    token = codec().encrypt(json.dumps(claims).encode()).decode()
    if not preview_domain():
        # Signed sandbox identity carries the user, runtime generation and port.
        # Path capabilities work in opaque-origin frames without login cookies.
        prefix = f"/preview/{token}/{port}/"
        origin = claims["browser_origin"]
        return {
            "url": origin + prefix + path.lstrip("/"),
            "forwarding_url": origin + prefix,
            "port": port,
            "path": path,
            "expires_in": TTL,
        }
    origin = f"https://{hostname(token)}"
    return {
        "url": f"{origin}/_flowork_preview/open?token={token}",
        "forwarding_url": origin + "/",
        "port": port,
        "path": path,
        "expires_in": TTL,
    }


def connection_claims(connection):
    if "service_preview_claims" in connection.scope:
        return connection.scope["service_preview_claims"]
    return claims_for(connection.cookies.get(COOKIE, ""), connection.url.hostname)


async def authorized_session(claims):
    async with session_scope(claims["tenant"], claims["user"]) as db:
        exists = (
            await db.execute(
                select(ChatProject.project_id).where(
                    ChatProject.project_id == claims["project"],
                    ChatProject.creator_user_id == claims["user"],
                    ChatProject.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(404, "service_forwarding_project_unavailable")
    session = await get_sandbox_manager().get_loaded_session(claims["tenant"], claims["scope"])
    if session is None:
        raise HTTPException(409, "service_preview_sandbox_stopped")
    return session


def upstream_request(connection, claims, *, websocket=False):
    headers = dict(connection.headers)
    excluded = HOP | {v.strip().lower() for v in headers.get("connection", "").split(",")}
    excluded |= {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"}
    headers = {k: v for k, v in headers.items() if k not in excluded}
    cookies = "; ".join(f"{k}={v}" for k, v in connection.cookies.items() if k != COOKIE)
    headers.pop("cookie", None)
    if cookies:
        headers["cookie"] = cookies
    if connection.scope.get("service_preview_prefix"):
        headers.pop("cookie", None)
        headers.pop("authorization", None)
        headers["accept-encoding"] = "identity"
    headers.pop("if-none-match", None)
    headers.pop("if-modified-since", None)
    headers.pop("forwarded", None)
    headers = {k: v for k, v in headers.items() if not k.startswith("x-forwarded-")}
    # Local development servers validate Host/Origin and do not know the proxy.
    if "origin" in headers:
        host = claims.get("loopback_host", "127.0.0.1")
        authority = "[::1]" if host == "::1" else host
        headers["origin"] = f"{claims['scheme']}://{authority}:{claims['port']}"
    raw_path = connection.scope.get("raw_path", b"/").decode("ascii")
    query = connection.scope.get("query_string", b"").decode("ascii")
    return dict(
        identifier="preview-" + uuid.uuid4().hex,
        runtime_id=claims["runtime_id"],
        port=claims["port"],
        scheme=claims["scheme"],
        loopback_host=claims.get("loopback_host", "127.0.0.1"),
        path=raw_path + ("?" + query if query else ""),
        method=getattr(connection, "method", "GET"),
        headers=headers,
        websocket=websocket,
    )


@gateway.get("/_flowork_preview/open")
async def bootstrap(request: Request, token: str):
    claims = claims_for(token, request.url.hostname)
    await authorized_session(claims)
    response = RedirectResponse(claims["path"], status_code=303)
    response.set_cookie(COOKIE, token, max_age=TTL, secure=True, httponly=True, samesite="none", path="/")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@gateway.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str):
    claims = connection_claims(request)
    prefix = request.scope.get("service_preview_prefix")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") not in (
        {"null"} if prefix else {f"https://{request.url.netloc}"}
    ):
        raise HTTPException(403, "invalid_forwarding_origin")
    if prefix and request.method == "OPTIONS":
        return Response(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "content-type"),
            }
        )
    session = await authorized_session(claims)
    payload = upstream_request(request, claims)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8 * 1024 * 1024:
            raise HTTPException(413, "service_preview_request_too_large")
    payload["body"] = base64.b64encode(body).decode()
    stream = session.service_forwarding_stream(payload)
    try:
        async with asyncio.timeout(35):
            first = await anext(stream)
        if first.get("type") != "headers":
            raise HTTPException(502, "service_preview_connection_failed: " + first.get("message", "no response"))
    except HTTPException:
        await stream.aclose()
        raise
    except (RuntimeError, OSError, TimeoutError) as exc:
        await stream.aclose()
        raise HTTPException(502, "service_preview_unavailable: " + str(exc)) from exc
    except BaseException:
        await stream.aclose()
        raise

    async def content():
        async with aclosing(stream):
            async with asyncio.timeout(max(1, claims["exp"] - time.time())):
                async for item in stream:
                    if item["type"] == "data":
                        yield base64.b64decode(item["data"])
                    elif item["type"] == "error":
                        raise ConnectionError(item["message"])

    content_type = next((v for k, v in first["headers"] if k.lower() == "content-type"), "")
    if prefix and any(kind in content_type for kind in ("text/html", "text/css")):
        document = bytearray()
        async for chunk in content():
            document.extend(chunk)
            if len(document) > 8 * 1024 * 1024:
                await stream.aclose()
                raise HTTPException(502, "service_preview_document_too_large")
        response = Response(
            rewrite_document(bytes(document), content_type, prefix, payload["path"]), status_code=first["status"]
        )
    else:
        response = StreamingResponse(content(), status_code=first["status"])
    response_hop = HOP | {
        part.strip().lower()
        for key, value in first["headers"]
        if key.lower() == "connection"
        for part in value.split(",")
    }
    for key, value in first["headers"]:
        lower = key.lower()
        if lower in response_hop | {"x-frame-options", "cache-control"}:
            continue
        if prefix and lower in {
            "set-cookie",
            "access-control-allow-origin",
            "access-control-allow-credentials",
            "refresh",
            "service-worker-allowed",
        }:
            continue
        if prefix and lower == "location":
            response.headers.append(key, redirect_url(value, prefix, claims["port"]))
            continue
        if lower == "set-cookie":
            cookies = SimpleCookie()
            try:
                cookies.load(value)
            except CookieError:
                continue
            for name, cookie in cookies.items():
                if name != COOKIE:
                    cookie["domain"] = ""
                    cookie["secure"] = True
                    response.headers.append("set-cookie", cookie.OutputString())
            continue
        if lower == "location":
            try:
                target, target_port = local_service_url(value)
                if target_port == claims["port"]:
                    value = target.path or "/"
                    if target.query:
                        value += "?" + target.query
                    if target.fragment:
                        value += "#" + target.fragment
            except ValueError:
                pass
        response.headers.append(key, value)
    # Never let generated code install persistent interceptors or embed itself
    # into arbitrary sites. It runs on its own origin, not the platform origin.
    public = urlsplit(claims.get("browser_origin") or config.public_urls.public_url)
    ancestors = [f"{public.scheme}://{public.netloc}"] if public.netloc else []
    if re.fullmatch(r"[a-p]{32}", config.browser_extension_id):
        ancestors.append(f"chrome-extension://{config.browser_extension_id}")
    frame_ancestors = " ".join(ancestors) or "'none'"
    response.headers.append("Content-Security-Policy", f"worker-src 'none'; frame-ancestors {frame_ancestors}")
    if prefix:
        origin = f"{public.scheme}://{public.netloc}"
        resource = origin + prefix
        websocket_resource = ("wss" if public.scheme == "https" else "ws") + "://" + public.netloc + prefix
        response.headers.append(
            "Content-Security-Policy",
            (
                "sandbox allow-scripts allow-forms allow-downloads; "
                f"default-src {resource} data: blob:; script-src {resource} 'unsafe-inline'; "
                f"style-src {resource} 'unsafe-inline'; connect-src {resource} {websocket_resource}; "
                f"base-uri {resource}; form-action {resource}; object-src 'none'; worker-src 'none'"
            ),
        )
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@gateway.websocket("/{path:path}")
async def websocket_proxy(ws: WebSocket, path: str):
    close_code = 1000
    try:
        claims = connection_claims(ws)
        prefix = ws.scope.get("service_preview_prefix")
        if ws.headers.get("origin") not in ({"null"} if prefix else {f"https://{ws.url.netloc}"}):
            raise HTTPException(403, "invalid_preview_origin")
        session = await authorized_session(claims)
        payload = upstream_request(ws, claims, websocket=True)
        payload["subprotocols"] = ws.scope.get("subprotocols", [])
        async with aclosing(session.service_forwarding_stream(payload)) as stream:
            async with asyncio.timeout(20):
                first = await anext(stream)
            if first["type"] != "ready":
                await ws.close(code=1011, reason="Preview service connection failed")
                return
            await ws.accept(subprotocol=first.get("subprotocol"))

            async def from_browser():
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    binary = message.get("bytes") is not None
                    data = message["bytes"] if binary else message["text"].encode()
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError("preview message too large")
                    await session.service_forwarding_control(
                        payload["identifier"],
                        {"action": "data", "binary": binary, "data": base64.b64encode(data).decode()},
                    )

            async def to_browser():
                async for message in stream:
                    if message["type"] == "data":
                        data = base64.b64decode(message["data"])
                        if message.get("binary"):
                            await ws.send_bytes(data)
                        else:
                            await ws.send_text(data.decode())
                        await session.service_forwarding_control(payload["identifier"], {"action": "ack"})
                    elif message["type"] in {"end", "error"}:
                        return

            tasks = [asyncio.create_task(from_browser()), asyncio.create_task(to_browser())]
            try:
                async with asyncio.timeout(max(1, claims["exp"] - time.time())):
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        await task
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except HTTPException:
        close_code = 1008
    except WebSocketDisconnect:
        pass
    except (TimeoutError, RuntimeError, OSError, ValueError):
        close_code = 1011
    finally:
        with suppress(RuntimeError):
            await ws.close(code=close_code)


class SandboxForwardingHostMiddleware:
    """Route ONLY dedicated preview hosts; platform routes stay unreachable there."""

    def __init__(self, app):
        self.app = app
        self.domain = preview_domain()

    async def __call__(self, scope, receive, send):
        domain = self.domain
        if scope["type"] in {"http", "websocket"} and scope.get("path", "").startswith("/preview/"):
            matched = re.fullmatch(r"/preview/([A-Za-z0-9_=-]+)/([0-9]+)/(.*)", scope["path"])
            try:
                if not matched:
                    raise ValueError("invalid service path")
                token, port, path = matched.groups()
                claims = json.loads(codec().decrypt(token.encode(), ttl=TTL))
                if claims["port"] != int(port) or claims["exp"] <= time.time():
                    raise ValueError("expired service path")
            except (InvalidToken, ValueError, KeyError, TypeError):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await HTMLResponse("Service preview expired. Reopen it from the conversation.", status_code=401)(
                        scope, receive, send
                    )
                return
            prefix = f"/preview/{token}/{port}/"
            forwarded = dict(
                scope,
                path="/" + path,
                raw_path=b"/" + scope.get("raw_path", b"")[len(prefix) :],
                service_preview_claims=claims,
                service_preview_prefix=prefix,
            )
            await gateway(forwarded, receive, send)
            return
        if domain and scope["type"] in {"http", "websocket"}:
            host = dict(scope.get("headers", [])).get(b"host", b"").decode().split(":", 1)[0].lower()
            if host == domain or host.endswith("." + domain):
                await gateway(scope, receive, send)
                return
        await self.app(scope, receive, send)
