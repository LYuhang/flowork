"""Request-ID middleware that propagates X-Request-ID and binds
correlation contextvars for the duration of the request. Fail-safe: any error
here must not break the request.

This is pure ASGI middleware, not Starlette ``BaseHTTPMiddleware``.
``BaseHTTPMiddleware`` wraps the downstream app in an anyio task + memory stream
and is known to break ``text/event-stream`` backpressure: it buffers the
streaming body and releases every chunk at the end (collapsing SSE into a single
late blob). The app has live SSE endpoints (chat / executions / tasks streams),
so we MUST stay out of the BaseHTTPMiddleware machinery. A plain ASGI callable
that injects/reads the header via ``scope``/``send`` adds no buffering and lets
``StreamingResponse`` chunks flow as they are produced. Verified by
``tests/observability/test_sse_streaming.py`` (chunks arrive incrementally).
"""
from __future__ import annotations

import uuid
from time import perf_counter

from vibecanvas_api.observability import context


class RequestIdMiddleware:
    """Pure-ASGI middleware. Reads/generates ``X-Request-ID``, binds the
    correlation contextvars for the request scope, and echoes the header back
    on the response start message — without touching the response body, so SSE
    streaming is preserved."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # websockets / lifespan pass straight through untouched.
            await self.app(scope, receive, send)
            return

        started_at = perf_counter()
        request_id = _request_id_from_scope(scope) or uuid.uuid4().hex
        # tenant_id is resolved later in the auth dependency; bind what we have.
        tokens = context.bind_request_context(request_id=request_id, tenant_id=None)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                # Echo X-Request-ID onto the outgoing headers. headers is a list
                # of (bytes, bytes) tuples; mutate a copy so we don't disturb the
                # downstream message object.
                headers = list(message.get("headers", []))
                headers = [
                    (k, v) for (k, v) in headers if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                # Expose application time separately from proxy/network wait
                # in browser Resource Timing. Stop at headers: SSE duration is
                # the lifetime of a stream, not request processing latency.
                headers.append((
                    b"server-timing",
                    f"app;dur={(perf_counter() - started_at) * 1000:.1f}".encode("ascii"),
                ))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            context.reset_request_context(tokens)


def _request_id_from_scope(scope) -> str | None:
    """Extract an inbound X-Request-ID header value from the ASGI scope."""
    for key, value in scope.get("headers", []):
        if key.lower() == b"x-request-id":
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None


def install_request_id_middleware(app) -> None:
    app.add_middleware(RequestIdMiddleware)
