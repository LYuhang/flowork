"""Standalone DBOS worker process for durable Flowork background work."""

from __future__ import annotations

import signal
import threading

from dbos import DBOS

from vibecanvas_api.config import config
from vibecanvas_api.security_profile import (
    configured_cors_origins,
    validate_production_security,
)
from vibecanvas_api.services.background_queue import (
    BACKGROUND_APPLICATION_NAME,
    QUEUE_SPECS,
)


def _dbos_config() -> dict[str, object]:
    return {
        "name": BACKGROUND_APPLICATION_NAME,
        "system_database_url": config.dbos_system_database_url,
        "sys_db_pool_size": config.dbos_worker_pool_size,
        "max_executor_threads": config.dbos_max_executor_threads,
        "use_listen_notify": True,
        "run_migrations": config.dbos_run_migrations,
        "console_log_level": config.observability.log_level.upper(),
    }


def main() -> None:
    validate_production_security(config, cors_origins=configured_cors_origins())
    DBOS(config=_dbos_config())

    # Import after constructing the singleton so decorators are registered
    # against this worker application and never against the API process.
    from vibecanvas_api.background_workflows import RETIRED_SCHEDULES, SCHEDULES
    from vibecanvas_api.observability.background_worker import (
        init_worker_observability,
        start_worker_metrics_server,
    )

    init_worker_observability()
    DBOS.launch()
    for spec in QUEUE_SPECS.values():
        DBOS.register_queue(
            spec.name,
            worker_concurrency=spec.worker_concurrency,
            polling_interval_sec=spec.polling_interval_sec,
        )
    for schedule_name in RETIRED_SCHEDULES:
        DBOS.delete_schedule(schedule_name)
    DBOS.apply_schedules(SCHEDULES)
    start_worker_metrics_server()

    stopped = threading.Event()

    def _stop(_signum, _frame) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        stopped.wait()
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=30)


if __name__ == "__main__":
    main()
