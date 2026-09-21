"""OpenTelemetry tracing with a set-once provider and auto-instrumentors.

OTLP HTTP export is environment-gated. Instrumentation failures are swallowed."""
from __future__ import annotations

import logging

from vibecanvas_api.config import config
from vibecanvas_api.observability.otel import trace

_log = logging.getLogger(__name__)
_INITIALIZED = False
_PROVIDER = None

# SSE routes: excluding by path (method-agnostic). Two of these paths are
# shared with non-streaming GETs (history/list) — we accept that collateral
# exclusion (review §5 option a). The clean-suffix ones (/stream, /resume)
# are precise.
SSE_EXCLUDED_URLS = ",".join([
    r"/stream",
    r"/resume",
    r"/messages",      # POST chat-stream shares path w/ GET history — collateral
    r"/executions",    # POST exec-stream shares path w/ GET list — collateral
])


def init_tracing() -> None:
    global _INITIALIZED, _PROVIDER
    if _INITIALIZED:
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "vibecanvas-api"})
        _PROVIDER = TracerProvider(resource=resource)
        if config.observability.otel_traces_enabled:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            _PROVIDER.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(_PROVIDER)
        _INITIALIZED = True  # commit guard+provider BEFORE instrumentors
    except Exception:  # fail-safe: never block startup on observability
        _log.exception("init_tracing failed; continuing without tracing")
        return
    try:
        _install_auto_instrumentors()
    except Exception:  # instrumentor failure must not cause provider churn
        _log.exception("auto-instrumentors failed; tracing provider still active")


def _install_auto_instrumentors() -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    # FastAPIInstrumentor is applied per-app in install_http_observability with
    # excluded_urls; here we cover the process-global instrumentors.
    for inst in (SQLAlchemyInstrumentor, RedisInstrumentor, HTTPXClientInstrumentor):
        try:
            inst().instrument()
        except Exception:
            _log.exception("instrumentor %s failed", inst.__name__)


def instrument_fastapi_app(app) -> None:
    """Called from install_http_observability() in build_app(); creates a
    per-request HTTP server span and excludes SSE routes from request spans.

    Each build_app() returns a fresh FastAPI instance, so instrumenting two
    apps never collides. We still guard on the OTel per-app flag so an
    accidental double-instrument of the same instance is a harmless no-op
    (FastAPIInstrumentor.instrument_app already checks this internally; the
    explicit guard makes the contract obvious and version-robust)."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        if getattr(app, "_is_instrumented_by_opentelemetry", False):
            return
        FastAPIInstrumentor.instrument_app(app, excluded_urls=SSE_EXCLUDED_URLS)
    except Exception:
        _log.exception("FastAPI instrumentation failed")


def add_span_processor_for_tests(processor) -> None:
    """Test hook: attach an extra processor (e.g. InMemorySpanExporter) to the
    live provider without replacing it (provider is set-once)."""
    if _PROVIDER is not None:
        _PROVIDER.add_span_processor(processor)
