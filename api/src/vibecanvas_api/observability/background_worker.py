"""Background-worker observability initialization.

The DBOS worker is a separate process and initializes independently. Metrics
are exposed from that single process on a dedicated endpoint. Fail-safe.
"""
from __future__ import annotations

import glob
import logging
import os

from vibecanvas_api.observability.logging import configure_logging
from vibecanvas_api.observability.tracing import init_tracing

_log = logging.getLogger(__name__)


def _clear_multiproc_dir() -> None:
    d = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not d:
        return
    os.makedirs(d, exist_ok=True)
    for f in glob.glob(os.path.join(d, "*.db")):
        try:
            os.remove(f)
        except OSError:
            pass


def init_worker_observability() -> None:
    """Called from worker_process_init: logging + tracing + instrumentors.
    Clears the multiprocess metrics dir so a restart doesn't show ghost series."""
    try:
        _clear_multiproc_dir()
        configure_logging()
        init_tracing()  # installs SQLAlchemy/Redis/httpx instrumentors
    except Exception:
        _log.exception("init_worker_observability failed; worker continues")


def start_worker_metrics_server() -> None:
    """Called from worker_ready in the PARENT process: serve aggregated
    multiprocess metrics on a dedicated port for Prometheus to scrape."""
    try:
        multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
        if not multiproc_dir:
            _log.info(
                "worker metrics server skipped: PROMETHEUS_MULTIPROC_DIR is not set"
            )
            return
        os.makedirs(multiproc_dir, exist_ok=True)
        port = int(os.environ.get("WORKER_METRICS_PORT", "9100"))
        # Imported inside the function: prometheus_client.multiprocess reads
        # PROMETHEUS_MULTIPROC_DIR at import/collect time, so deferring the
        # import keeps the env contract explicit (mirrors tracing.py's
        # sanctioned conditional-import discipline).
        from prometheus_client import (
            CollectorRegistry,
            multiprocess,
            start_http_server,
        )

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        start_http_server(port, registry=registry)
        _log.info("worker metrics server on :%d", port)
    except Exception:
        _log.exception("start_worker_metrics_server failed")
