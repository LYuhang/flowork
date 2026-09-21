"""FastAPI application factory.

Exposes ``build_app()`` returning a configured FastAPI instance.
``cli.py`` calls this directly (no module-level singleton — tests
build their own app instance).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .observability import configure_logging, init_tracing, install_http_observability

# Configure structured logging at import time, before uvicorn boots
# its own dictConfig (uvicorn imports this module via the app factory, so this
# runs first and our handlers win). Fail-safe inside configure_logging().
configure_logging()


async def _sandbox_idle_reaper_loop(stop: asyncio.Event, interval_s: float) -> None:
    """Periodically reap idle resident sandboxes while the API process is alive."""
    from .services.sandbox.manager import get_existing_sandbox_manager

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            break
        except asyncio.TimeoutError:
            pass
        try:
            mgr = get_existing_sandbox_manager()
            if mgr is not None:
                await mgr.sweep_idle()
        except Exception:
            import structlog

            structlog.get_logger(__name__).warning(
                "sandbox_idle_reaper_failed", exc_info=True
            )


def _resolve_api_root() -> Path:
    """Resolve the api/ root that owns alembic.ini and alembic/.

    Editable/source runs can infer this from ``app.py``. Non-editable installs
    cannot: ``__file__`` then points inside site-packages, while Alembic files
    remain in the repo/deployment root. Prefer explicit startup env and the
    process cwd before falling back to source-relative guesses.
    """
    candidates: list[Path] = []
    if os.getenv("VIBECANVAS_API_ROOT"):
        candidates.append(Path(os.environ["VIBECANVAS_API_ROOT"]))
    cwd = Path.cwd()
    candidates.extend((cwd, cwd / "api"))
    here = Path(__file__).resolve()
    candidates.extend(here.parents)
    candidates.append(Path("/app"))
    for root in candidates:
        root = root.resolve()
        if (root / "alembic.ini").is_file() and (root / "alembic").is_dir():
            return root
    checked = ", ".join(str(p) for p in candidates)
    raise RuntimeError(
        "could not locate api alembic root; set VIBECANVAS_API_ROOT "
        f"(checked: {checked})"
    )


def _parse_cors_origins() -> list[str]:
    """Parse VIBECANVAS_API_CORS_ORIGINS env var; default localhost:3000."""
    from .security_profile import configured_cors_origins

    return configured_cors_origins()


def _run_migrations_sync() -> None:
    """Upgrade the database to the Alembic head (BLOCKING, no running
    event loop expected).

    Idempotent — Alembic's ``alembic_version`` table makes re-running
    ``upgrade head`` a no-op when already current (the conftest
    ``_migrate`` fixture migrates first; the lifespan re-running here
    must not double-DDL, and does not). ``alembic/env.py`` reads
    ``config.database.url`` (the live singleton), so the URL is
    whatever the process/config resolved (test fixtures mutate the
    singleton in-memory before build_app()).

    MUST run with NO running event loop: ``alembic/env.py`` ends with
    ``asyncio.run(run_async_migrations())``; ``asyncio.run`` raises if
    called from inside a running loop. The lifespan runs inside one
    (uvicorn / Starlette TestClient), so the async lifespan offloads
    this to a worker thread via ``asyncio.to_thread`` — the thread has
    no running loop, so env.py's ``asyncio.run`` works.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    api_root = _resolve_api_root()
    cfg = AlembicConfig(str(api_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_root / "alembic"))
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: migrate, initialize the database, then wire shared stores.

    Shutdown drains in-flight agent turns before closing the SQLAlchemy engine.

    Every acquired resource is registered with an ``AsyncExitStack`` before
    later startup work, so a partial startup still unwinds cleanly.
    """
    from .config import config as app_config
    from .context import init_stores

    # Production file ingress is fail-closed before the API becomes ready. A
    # later scanner outage is still checked on every upload.
    from .security.upload_scanner import probe_upload_scanner
    from .storage.db import (
        dispose_engine,
        init_engine,
        maintenance_database_url,
    )
    from .storage.vfs_store import PostgresVfsStore

    await probe_upload_scanner()

    # Strict v2 Runtime authentication is broker-only. Remove the exact legacy
    # plaintext Codex cache before any sandbox can be created; there is no
    # compatibility reader for this layout.
    from .security.legacy_runtime_cleanup import purge_legacy_codex_auth_files

    removed_codex_auth_files = await asyncio.to_thread(
        purge_legacy_codex_auth_files,
        app_config.agent_runtime_root,
    )
    if removed_codex_auth_files:
        import structlog

        structlog.get_logger(__name__).warning(
            "legacy_codex_plaintext_auth_removed",
            removed_count=removed_codex_auth_files,
        )

    # 1. Development/test may migrate on startup for convenience. Production
    #    defaults this off and uses the one-shot migration workload so the API
    #    process does not need schema-changing authority.
    if app_config.run_database_migrations:
        # Alembic calls asyncio.run(), so use a worker thread with no event loop.
        await asyncio.to_thread(_run_migrations_sync)
        # Alembic fileConfig replaces process-wide handlers; restore ours.
        configure_logging()

    # Set up the OTel TracerProvider and auto-instrumentors once during
    # lifespan startup (set-once + fail-safe; OTLP export is env-gated off by
    # default). Must run before any request is served.
    init_tracing()

    # UX-10e — warn loudly if the VFS signed-URL secret fell back to a
    # per-process random value. Such URLs do NOT survive a restart and are NOT
    # valid across replicas. Prod MUST set VIBECANVAS_SIGNING_SECRET.
    if app_config._signing_secret_is_ephemeral:
        import structlog

        structlog.get_logger(__name__).warning(
            "vfs_signing_secret_ephemeral",
            detail="VIBECANVAS_SIGNING_SECRET unset; using a per-process random "
            "secret. Signed media URLs break on restart / across replicas. "
            "Set it in production.",
        )

    # P1 — set the host-level sandbox concurrency cap once at startup from config.
    # Node debug execution still acquires this admission semaphore around direct
    # provider calls.
    from .services.sandbox.admission import configure_admission

    configure_admission(app_config.sandbox_max_concurrent)

    async with contextlib.AsyncExitStack() as stack:
        # The API only owns a lightweight DBOS client for durable enqueue and
        # cancellation. Close its connection pool after request producers stop.
        from .services.background_queue import close_background_queue_client

        stack.callback(close_background_queue_client)
        # 2. Main SQLAlchemy async engine (request DI path). Register
        #    dispose FIRST so any later failure still tears it down.
        runtime_engine = init_engine()
        stack.push_async_callback(dispose_engine)
        if app_config.environment == "production":
            from sqlalchemy import NullPool
            from sqlalchemy.ext.asyncio import create_async_engine

            from .security.database_privileges import verify_database_role

            await verify_database_role(runtime_engine, mode="runtime")
            maintenance_engine = create_async_engine(
                maintenance_database_url(),
                poolclass=NullPool,
                connect_args={"prepared_statement_cache_size": 0},
            )
            try:
                await verify_database_role(maintenance_engine, mode="maintenance")
            finally:
                await maintenance_engine.dispose()

        # OpenFGA is a pinned, mandatory control-plane dependency. Probe the
        # exact immutable model before accepting requests; startup fails if
        # the authoritative control plane is unavailable.
        from .authorization.openfga_client import OpenFgaHttpClient

        openfga_client = OpenFgaHttpClient(
            api_url=app_config.openfga_api_url,
            store_id=app_config.openfga_store_id,
            authorization_model_id=(app_config.openfga_authorization_model_id),
            api_token=app_config.openfga_api_token,
            timeout_seconds=app_config.openfga_timeout_seconds,
        )
        stack.push_async_callback(openfga_client.close)
        await openfga_client.probe()
        app.state.openfga_client = openfga_client

        # Platform tools execute behind the private Host Capability Gateway.
        # Their canonical invocation layer shares the API lifespan's OpenFGA
        # client, but no model-visible Platform MCP transport runs on the Host.
        from .services.platform_mcp.invocation import (
            set_platform_mcp_openfga_client,
        )

        set_platform_mcp_openfga_client(getattr(app.state, "openfga_client", None))
        stack.callback(set_platform_mcp_openfga_client, None)

        # Initialize process-scoped stores after the durable task subsystem.
        init_stores(
            _vfs_store=PostgresVfsStore(),
        )

        # Optional self-hosted MOUNT_PATH bridge. Registrations are created
        # only for authenticated users/sandboxes; the watcher then mirrors host
        # file changes into encrypted VFS rows and their durable Preview cursor.
        from .services.user_mount_workspace import host_mount_bridge

        await host_mount_bridge.start()
        stack.push_async_callback(host_mount_bridge.shutdown)

        # Fail closed before accepting HTTP traffic.  In service mode the API
        # must never silently create a process-local sandbox when sandboxd is
        # absent; launchers and orchestrators can therefore trust readiness.
        from .services.sandbox.manager import get_sandbox_manager

        sandbox_manager = get_sandbox_manager()
        health = getattr(sandbox_manager, "health", None)
        if health is not None:
            await health()

        sandbox_reaper_stop = asyncio.Event()
        sandbox_reaper_interval = max(
            5.0,
            min(60.0, float(getattr(app_config, "sandbox_idle_ttl_s", 600)) / 2.0),
        )
        sandbox_reaper_task = asyncio.create_task(
            _sandbox_idle_reaper_loop(sandbox_reaper_stop, sandbox_reaper_interval)
        )
        stack.push_async_callback(
            _stop_sandbox_runtime, sandbox_reaper_stop, sandbox_reaper_task
        )
        # Some dependency initializers used above may configure stdlib logging
        # as a side effect. This is the final startup boundary immediately
        # before requests are accepted, so make the product pipeline
        # authoritative here as well.
        configure_logging()
        try:
            yield
        finally:
            # Shutdown: drain in-flight agent turns before the database engine.
            from .streaming.turn_runtime import TURN_STOP, TURN_TASKS

            for ev in TURN_STOP.values():
                ev.set()
            for task in TURN_TASKS.values():
                if not task.done():
                    try:
                        await task
                    except Exception:
                        pass

            # Detach the service-locator references before the exit stack
            # closes their pools.  Otherwise a later in-process app lifespan
            # (or a lifespan-less route test) can observe a closed saver.
            from .context import clear_stores

            clear_stores()

            # AsyncExitStack unwinds registered resources here in LIFO order,
            # including the SQLAlchemy engine.


def build_app() -> FastAPI:
    from .config import config as app_config
    from .security_profile import validate_production_security

    cors_origins = _parse_cors_origins()
    # Validate before constructing routers or opening database
    # pools. A production process with a development fallback must never become
    # live enough to answer even a health check.
    validate_production_security(app_config, cors_origins=cors_origins)

    app = FastAPI(
        title="Flowork API",
        version=__version__,
        description="HTTP API for Flowork workflow editing and execution.",
        lifespan=lifespan,
        docs_url=None if app_config.environment == "production" else "/docs",
        redoc_url=None if app_config.environment == "production" else "/redoc",
        openapi_url=(
            None if app_config.environment == "production" else "/openapi.json"
        ),
    )

    from fastapi.responses import JSONResponse

    from .authorization.openfga_client import OpenFgaUnavailableError

    @app.exception_handler(OpenFgaUnavailableError)
    async def authorization_unavailable_handler(
        _request,
        _exc: OpenFgaUnavailableError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "authorization_unavailable",
                    "message": "Authorization is temporarily unavailable.",
                }
            },
            headers={"Retry-After": "1"},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    from .request_body_limit import RequestBodyLimitMiddleware

    app.add_middleware(
        RequestBodyLimitMiddleware,
        default_limit=app_config.http_request_max_bytes,
    )
    from .security_headers import SecurityHeadersMiddleware

    app.add_middleware(
        SecurityHeadersMiddleware,
        production=app_config.environment == "production",
    )

    # Mount routers
    from .routes import agent_runtime as _agent_runtime_routes
    from .routes import audit as _audit_routes
    from .routes import auth as _auth_routes
    from .routes import browser as _browser_routes
    from .routes import chats as _chat_routes
    from .routes import deployment_invoke as _deployment_invoke_routes
    from .routes import deployments as _deployment_routes
    from .routes import enterprise_identity as _enterprise_identity_routes
    from .routes import envs as _envs_routes
    from .routes import executions as _exec_routes
    from .routes import kb as _kb_routes
    from .routes import llm_credentials as _llm_credentials_routes
    from .routes import mcp_servers as _mcp_servers_routes
    from .routes import meta as _meta_routes
    from .routes import organizations as _organization_routes
    from .routes import platform_management as _platform_management_routes
    from .routes import previews as _preview_routes
    from .routes import resource_access as _resource_access_routes
    from .routes import privileged_access as _privileged_access_routes
    from .routes import runtime_mcp_broker as _runtime_mcp_broker_routes
    from .routes import runtime_model_broker as _runtime_model_broker_routes
    from .routes import scim as _scim_routes
    from .routes import skills as _skills_routes
    from .routes import storage as _storage_routes
    from .routes import tasks as _task_routes
    from .routes import vfs as _vfs_routes
    from .routes import webauthn as _webauthn_routes
    from .routes import workflows as _workflow_routes

    app.add_exception_handler(
        _scim_routes.ScimProtocolError,
        _scim_routes.scim_exception_handler,
    )
    app.include_router(_auth_routes.router)
    app.include_router(_webauthn_routes.router)
    app.include_router(_privileged_access_routes.router)
    app.include_router(_platform_management_routes.router)
    app.include_router(_organization_routes.router)
    app.include_router(_meta_routes.router)
    app.include_router(_workflow_routes.router)
    app.include_router(_exec_routes.router)
    app.include_router(_chat_routes.router)
    app.include_router(_task_routes.router)
    app.include_router(_deployment_routes.router)
    app.include_router(_deployment_invoke_routes.router)
    app.include_router(_mcp_servers_routes.router)
    app.include_router(_skills_routes.router)
    app.include_router(_llm_credentials_routes.router)
    app.include_router(_kb_routes.router)
    app.include_router(_audit_routes.router)
    app.include_router(_vfs_routes.router)
    app.include_router(_preview_routes.router)
    app.include_router(_resource_access_routes.router)
    app.include_router(_storage_routes.router)
    app.include_router(_envs_routes.router)
    app.include_router(_browser_routes.router)
    app.include_router(_agent_runtime_routes.router)
    app.include_router(_runtime_mcp_broker_routes.router)
    app.include_router(_runtime_model_broker_routes.router)
    app.include_router(_enterprise_identity_routes.router)
    app.include_router(_scim_routes.router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        """Liveness probe. No auth required."""
        return {"status": "ok"}

    # Install request-id middleware (pure ASGI and SSE-safe), metrics,
    # + FastAPI request tracing (SSE routes excluded). Installed after routers
    # so it wraps the full app; idempotent across repeated build_app() calls
    # (metrics are module-level singletons). Fail-safe inside.
    install_http_observability(app)
    # A route that is not in the typed authorization inventory is a startup
    # error, not an implicitly tenant-visible endpoint.
    from .authorization.manifest import route_permission_manifest

    route_permission_manifest(app)

    return app


async def _stop_sandbox_runtime(
    reaper_stop: asyncio.Event,
    reaper_task: asyncio.Task,
) -> None:
    """Stop sandbox background services and release resident workers."""
    reaper_stop.set()
    if not reaper_task.done():
        reaper_task.cancel()
        with contextlib.suppress(BaseException):
            await reaper_task
    try:
        from .services.sandbox.manager import (
            clear_sandbox_manager,
            get_existing_sandbox_manager,
        )

        mgr = get_existing_sandbox_manager()
        if mgr is not None:
            await mgr.shutdown()
            clear_sandbox_manager(mgr)
    except Exception:
        import structlog

        structlog.get_logger(__name__).warning(
            "sandbox_manager_shutdown_failed", exc_info=True
        )
