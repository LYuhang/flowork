"""T8 — SSE streaming must survive the request_id middleware.

The carried-forward T2 review concern: starlette's ``BaseHTTPMiddleware`` was
historically known to buffer / break ``text/event-stream`` responses. The app
has live SSE endpoints (chat / executions / tasks streams), so the request_id
middleware MUST NOT introduce buffering.

How we test it precisely: we drive the ASGI app directly with a recording
``send`` callable that timestamps every ``http.response.body`` message the app
emits. A generator that sleeps ``_CHUNK_DELAY`` between yields, when NOT
buffered, makes the body messages arrive spread out over wall-clock time. If a
middleware buffered, every body message would be released at the end and the
inter-message spread would collapse to ~0.

(We assert at the ASGI ``send`` boundary, not via an HTTP client, because
``httpx.ASGITransport`` itself collects the whole response before exposing it to
``aiter_lines`` — a client-side artifact that masks the real behaviour. The
original "spread 0.000s" reading that looked like middleware buffering was in
fact this ASGITransport artifact.)

FINDING (T8): on the pinned starlette 0.48, even ``BaseHTTPMiddleware`` streams
SSE chunks through incrementally at the ASGI boundary (the anyio memory-stream
rewrite shipped in starlette ~0.21 fixed the old buffering). We nonetheless
ship ``RequestIdMiddleware`` as a **pure-ASGI** middleware: it is the
recommended SSE-safe pattern, carries zero buffering risk, avoids the
BaseHTTPMiddleware anyio task-group overhead per request, and is immune to
future regressions. The control test below documents the current
BaseHTTPMiddleware behaviour so a future regression is caught.
"""
import asyncio
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import StreamingResponse

from vibecanvas_api.observability.middleware import RequestIdMiddleware

_CHUNK_DELAY = 0.15  # seconds between yields
_N_CHUNKS = 4


async def _sse_app(scope, receive, send):
    """Bare ASGI app emitting an SSE stream with a delay between chunks."""
    async def gen():
        for i in range(_N_CHUNKS):
            yield f"data: chunk-{i}\n\n".encode()
            await asyncio.sleep(_CHUNK_DELAY)

    response = StreamingResponse(gen(), media_type="text/event-stream")
    await response(scope, receive, send)


async def _drive(asgi_app) -> tuple[list[float], dict]:
    """Run a GET /sse against the given ASGI app, recording the wall-clock
    arrival time of each non-empty response-body chunk + the response headers."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/sse",
        "headers": [],
        "query_string": b"",
    }

    # First call delivers the (empty) request body; subsequent calls block
    # until the response finishes, then report a disconnect. StreamingResponse
    # spawns a listen_for_disconnect task that awaits receive(); returning a
    # plain http.request on every call would spin it in a tight loop and hang.
    _request_sent = asyncio.Event()
    _done = asyncio.Event()

    async def receive():
        if not _request_sent.is_set():
            _request_sent.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await _done.wait()
        return {"type": "http.disconnect"}

    body_arrivals: list[float] = []
    captured = {"headers": {}}
    start = time.monotonic()

    async def send(message):
        if message["type"] == "http.response.start":
            captured["headers"] = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            if message.get("body"):
                body_arrivals.append(time.monotonic() - start)

    try:
        await asgi_app(scope, receive, send)
    finally:
        _done.set()
    return body_arrivals, captured["headers"]


def _assert_streams_incrementally(arrivals: list[float]) -> float:
    spread = arrivals[-1] - arrivals[0]
    return spread


def test_request_id_middleware_does_not_buffer_sse():
    """RequestIdMiddleware (pure ASGI) must let SSE body chunks stream
    incrementally and still echo X-Request-ID on the response."""
    app = RequestIdMiddleware(_sse_app)
    arrivals, headers = asyncio.run(_drive(app))

    assert len(arrivals) == _N_CHUNKS, arrivals
    assert "x-request-id" in headers
    assert headers["server-timing"].startswith("app;dur=")
    # The measurement is emitted with headers, before the delayed stream body.
    assert float(headers["server-timing"].split("=")[1]) < arrivals[-1] * 1000
    spread = _assert_streams_incrementally(arrivals)
    min_expected = _CHUNK_DELAY * (_N_CHUNKS - 2)
    assert spread >= min_expected, (
        f"RequestIdMiddleware buffers SSE: body-chunk spread {spread:.3f}s "
        f"< expected >= {min_expected:.3f}s; arrivals={arrivals}"
    )


def test_basehttpmiddleware_current_streaming_behaviour_control():
    """Control/regression: document how BaseHTTPMiddleware behaves on the
    pinned starlette. On starlette 0.48 it streams SSE chunks incrementally at
    the ASGI boundary (spread reflects the per-chunk delays). If a future
    starlette upgrade regresses this to buffering (spread → ~0), this control
    flips and signals that the pure-ASGI RequestIdMiddleware is load-bearing —
    do not revert it."""

    class _PassThroughBHM(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            return await call_next(request)

    app = _PassThroughBHM(_sse_app)
    arrivals, _ = asyncio.run(_drive(app))
    spread = arrivals[-1] - arrivals[0] if len(arrivals) >= 2 else 0.0
    min_expected = _CHUNK_DELAY * (_N_CHUNKS - 2)
    assert spread >= min_expected, (
        "REGRESSION: BaseHTTPMiddleware now buffers SSE on this starlette "
        f"(spread {spread:.3f}s < {min_expected:.3f}s). The pure-ASGI "
        f"RequestIdMiddleware is now load-bearing; arrivals={arrivals}"
    )
