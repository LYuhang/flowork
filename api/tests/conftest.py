"""Shared API integration-test fixtures.

Tests run against the installed vibecanvas_api package and a
tmp_path-rooted storage config — they never touch the user's
~/.vibecanvas-api-data/ or the legacy demo's local_data/.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from _pytest.monkeypatch import MonkeyPatch
from alembic import command
from alembic.config import Config as AlembicConfig
from pytest_postgresql import factories
from pytest_postgresql.janitor import DatabaseJanitor
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Importing the config module is side-effect-free and returns the same
# already-constructed singleton (test collection already imports it); only
# the `.url` *assignment* in `_migrate` is timing-sensitive (see _migrate).
from vibecanvas_api.app import build_app
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.config import config as _live_config
from vibecanvas_api.services.object_store import FilesystemObjectStore
from vibecanvas_api.services.sandbox import _resolve_runsc
from vibecanvas_api.storage.db import dispose_engine, session_scope
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo

# Unit/integration tests deliberately exercise the in-process implementation;
# service transport has its own contract suite. Runtime launchers and release
# manifests explicitly use the fail-closed service mode.
_live_config.sandbox_service_mode = "embedded"


TESTS_DIR = Path(__file__).resolve().parent
API_DIR = TESTS_DIR.parent
ALEMBIC_INI = API_DIR / "alembic.ini"
ALEMBIC_DIR = API_DIR / "alembic"

# Tables truncated between tests.
_TRUNCATE_TABLES = (
    "workflows, workflow_versions, chats, chat_messages, "
    "chat_tool_job_events, chat_tool_jobs, "
    "templates, executions, "
    "deployment_webhook_receipts, deployments, tasks, task_events, "
    "mcp_servers, "
    "env_builds, "
    "usage_events, usage_rollup_daily, "
    "oidc_login_transactions, enterprise_directory_users, "
    "enterprise_identity_providers, privileged_access_requests, sessions, "
    "platform_admin_eligibilities, "
    "password_reset_tokens, auth_identities, "
    "data_purge_jobs, content_encryption_keys, encrypted_secrets, "
    "account_deletion_requests, users, tenants"
)


@pytest.fixture(autouse=True)
def _isolate_local_auth_rate_limiter(monkeypatch):
    """Give every test the same fresh in-process auth limiter state.

    The production multi-replica path uses Redis.  Tests intentionally use
    the local fallback, whose module-level window would otherwise make
    unrelated tests sharing ASGITransport's loopback IP exhaust one another.
    """
    from vibecanvas_api.auth import ratelimit

    # Some authorization matrices deliberately create more than five users
    # behind one synthetic loopback address.  The production threshold itself
    # is covered by focused limiter tests; use headroom here so those matrices
    # test authorization, not the test transport's shared source address.
    monkeypatch.setattr(
        ratelimit,
        "_local_limiter",
        ratelimit.LoginRateLimiter(max_attempts=100, window_seconds=300),
    )
    monkeypatch.setattr(ratelimit, "_local_action_limiters", {})


@pytest.fixture(autouse=True)
def _isolate_sync_tenant_context():
    """Prevent one sync-worker test from leaking its RLS tenant to another."""
    from vibecanvas_api.storage.sync_session import current_sync_tenant_id

    token = current_sync_tenant_id.set(None)
    try:
        yield
    finally:
        current_sync_tenant_id.reset(token)


@pytest.fixture(autouse=True)
def _explicit_test_agent_runtime(monkeypatch):
    """Give integration tests an explicit fake platform model connection.

    Production deliberately has no implicit/default API. Route tests that
    exercise dispatch with a fake Runtime orchestrator still need a selectable
    model, so the test harness supplies one explicitly instead of weakening the
    product fail-closed default.
    """
    monkeypatch.setattr(_live_config.agent, "model", "openai:test-model")
    monkeypatch.setattr(_live_config.agent, "api_key", "test-provider-key")
    # Managed Codex profiles are parsed once when AppConfig starts. Updating
    # only the legacy AgentConfig fields no longer creates a selectable Runtime
    # model, so install the explicit host-brokered test profile as well.
    monkeypatch.setattr(
        _live_config,
        "codex_managed_apis",
        [
            {
                "id": "test-managed",
                "name": "Test OpenAI",
                "base_url": "https://api.openai.test/v1",
                "api_key": "test-provider-key",
                "models": ("test-model",),
            }
        ],
    )


@pytest.fixture
def tmp_storage_root(tmp_path: Path) -> Path:
    """Per-test fresh storage root."""
    root = tmp_path / "vibecanvas_test"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# runsc bootstrap for the guarded gVisor integration test (RE-6 P1 T2)
#
# runsc is NOT pip-installable and is NOT pre-installed on dev/CI nodes, so
# the headline gVisor test "passes by skipping" unless we fetch it. This
# session-scoped, autouse fixture fetches a PINNED + sha512-verified runsc
# (via scripts/get_runsc.sh) and points RUNSC_PATH at it BEFORE any test that
# calls `_gvisor_runnable()` runs — so T3 actually RUNS in this shell.
#
# Fail-soft: if runsc is already resolvable it does nothing; if the fetch
# fails (e.g. offline) it leaves runsc unresolved and the guarded test SKIPS
# cleanly. Idempotent + fast on re-run (get_runsc.sh reuses the cached binary).
# ---------------------------------------------------------------------------
_GET_RUNSC_SCRIPT = API_DIR.parent / "scripts" / "get_runsc.sh"


def _fetch_runsc_into_env() -> None:
    """Best-effort fetch of runsc + set RUNSC_PATH so the gVisor test can RUN."""
    if _resolve_runsc() is not None:
        return  # already on PATH / RUNSC_PATH / config — nothing to do.
    if not _GET_RUNSC_SCRIPT.exists():
        return
    try:
        proc = subprocess.run(
            ["bash", str(_GET_RUNSC_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception:
        return  # offline / no downloader — leave runsc unresolved (T3 skips).
    if proc.returncode != 0:
        return
    path = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if path and os.path.exists(path):
        os.environ["RUNSC_PATH"] = path


def pytest_configure(config) -> None:
    """Fetch runsc BEFORE collection.

    ``test_sandbox_gvisor.py`` guards itself with a module-level
    ``pytest.mark.skipif(not _gvisor_runnable())``. ``skipif`` is evaluated when
    the test module is *imported during collection* — strictly before any
    session-scoped fixture runs. So the fetch MUST happen in ``pytest_configure``
    (a pre-collection hook); doing it in a fixture would set ``RUNSC_PATH`` too
    late and the integration test would skip even though gVisor is runnable here.
    """
    _fetch_runsc_into_env()


@pytest.fixture(scope="session", autouse=True)
def _ensure_runsc() -> None:
    """Back-compat: re-assert the fetch for any non-collection-time caller."""
    _fetch_runsc_into_env()


# ---------------------------------------------------------------------------
# PostgreSQL test fixtures. The tests use a real local PostgreSQL server rather
# than a SQLite compatibility layer. Discover the executable so the suite works
# with the distribution-supported server version instead of one hard-coded
# Ubuntu release.
# ---------------------------------------------------------------------------

def _find_test_pg_ctl() -> str:
    configured = os.environ.get("FLOWORK_TEST_PG_CTL", "").strip()
    candidates = [Path(configured)] if configured else []
    on_path = shutil.which("pg_ctl")
    if on_path:
        candidates.append(Path(on_path))
    candidates.extend(
        sorted(Path("/usr/lib/postgresql").glob("*/bin/pg_ctl"), reverse=True)
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError(
        "PostgreSQL server binaries are required for API tests. Install the "
        "distribution postgresql package or set FLOWORK_TEST_PG_CTL."
    )


# port=None lets pytest-postgresql choose an available local port. WSL hosts
# with a mirrored Windows/VPN network can make port-for's localhost probe hang;
# CI or a developer may pin an isolated port without changing the fixture.
_TEST_POSTGRESQL_PORT = os.environ.get("FLOWORK_TEST_PG_PORT", "").strip()
if os.environ.get("FLOWORK_TEST_PG_URL", "").strip():
    @pytest.fixture(scope="session")
    def postgresql_proc():
        raise RuntimeError(
            "postgresql_proc is unavailable when FLOWORK_TEST_PG_URL is set"
        )
else:
    postgresql_proc = factories.postgresql_proc(
        executable=_find_test_pg_ctl(),
        # 127.0.0.1 may be intercepted by a mirrored Windows VPN and leave a
        # closed-port probe in SYN_SENT until timeout. Another loopback address
        # is still local-only but fails/accepts immediately on Linux and WSL.
        host=os.environ.get("FLOWORK_TEST_PG_HOST", "127.0.0.2"),
        port=int(_TEST_POSTGRESQL_PORT) if _TEST_POSTGRESQL_PORT else None,
        unixsocketdir=os.environ.get("FLOWORK_TEST_PG_SOCKET_DIR", "/tmp"),
        postgres_options=(
            "-c listen_addresses="
            + os.environ.get("FLOWORK_TEST_PG_HOST", "127.0.0.2")
        ),
    )


@pytest.fixture(scope="session")
def monkeypatch_session():
    """Session-scoped MonkeyPatch (built-in monkeypatch is function-scoped).
    Used to set DATABASE_URL for any subprocess / late importer."""
    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="session")
def pg_url(request) -> str:
    """Return a superuser URL for an isolated, disposable test database.

    The default starts a local ``pytest-postgresql`` process. A host which
    intentionally keeps PostgreSQL in Docker may set ``FLOWORK_TEST_PG_URL``
    to an existing cluster's administrative database. That mode creates and
    later drops only ``FLOWORK_TEST_PG_DATABASE``; the database named by the
    administrative URL is never migrated or truncated.
    """
    external_url = os.environ.get("FLOWORK_TEST_PG_URL", "").strip()
    dbname = os.environ.get(
        "FLOWORK_TEST_PG_DATABASE", "vibecanvas_test"
    ).strip()
    if not dbname or not dbname.replace("_", "").isalnum():
        raise RuntimeError(
            "FLOWORK_TEST_PG_DATABASE must contain only letters, digits, "
            "and underscores"
        )

    if external_url:
        admin_url = make_url(external_url)
        if not admin_url.database:
            raise RuntimeError("FLOWORK_TEST_PG_URL must name an admin database")
        sync_admin_url = admin_url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )

        def _drop_database(conn) -> None:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')

        with psycopg.connect(sync_admin_url, autocommit=True) as conn:
            _drop_database(conn)
            conn.execute(
                f'CREATE DATABASE "{dbname}" OWNER "vibecanvas_app"'
            )
        try:
            yield admin_url.set(
                drivername="postgresql+asyncpg", database=dbname
            ).render_as_string(hide_password=False)
        finally:
            with psycopg.connect(sync_admin_url, autocommit=True) as conn:
                _drop_database(conn)
        return

    # Resolve the process fixture lazily so external-cluster mode does not
    # require local PostgreSQL server binaries merely to import conftest.py.
    proc = request.getfixturevalue("postgresql_proc")
    with DatabaseJanitor(
        user=proc.user,
        host=proc.host,
        port=proc.port,
        version=proc.version,
        dbname=dbname,
        password=proc.password,
    ):
        pw = f":{proc.password}" if proc.password else ""
        yield (
            f"postgresql+asyncpg://{proc.user}{pw}"
            f"@{proc.host}:{proc.port}/{dbname}"
        )


@pytest.fixture(scope="session", autouse=True)
def _migrate(pg_url, monkeypatch_session):
    """Create the non-superuser ``vibecanvas_app`` role, then migrate the
    test DB to head AS that role so it OWNS every table.

    Postgres superusers bypass RLS unconditionally, so
    pytest-postgresql's bootstrap role is a superuser. The app (and these
    migrations) must run as a non-superuser role for RLS to apply.
    ``vibecanvas_app`` owns the business tables; ``FORCE ROW LEVEL
    SECURITY`` (migration 003) makes RLS bind even the owner.

    The config-singleton URL is repointed to ``vibecanvas_app`` BEFORE
    ``command.upgrade`` so alembic/env.py (which reads the singleton)
    connects as that role; ``db.py:init_engine`` then also builds the
    app engine as ``vibecanvas_app``.
    """
    su_url = make_url(pg_url)
    dbname = su_url.database
    external_cluster = bool(os.environ.get("FLOWORK_TEST_PG_URL", "").strip())
    app_password = (
        os.environ.get("FLOWORK_TEST_APP_PASSWORD", "").strip()
        if external_cluster
        else "vibecanvas_app"
    )
    if external_cluster and not app_password:
        raise RuntimeError(
            "FLOWORK_TEST_APP_PASSWORD is required with FLOWORK_TEST_PG_URL"
        )

    # 1. Local ephemeral clusters get a fresh app role. External-cluster mode
    # reuses and validates the deployment role; it must never drop or alter a
    # role that can own product data in another database.
    with psycopg.connect(pg_url.replace("+asyncpg", ""),
                         autocommit=True) as conn:
        if external_cluster:
            role = conn.execute(
                "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = 'vibecanvas_app'"
            ).fetchone()
            if role is None:
                raise RuntimeError(
                    "external test cluster must provide vibecanvas_app"
                )
            if not role[0] or role[1] or role[2]:
                raise RuntimeError(
                    "vibecanvas_app must be LOGIN, NOSUPERUSER, NOBYPASSRLS"
                )
        else:
            conn.execute("DROP ROLE IF EXISTS vibecanvas_app")
            conn.execute(
                "CREATE ROLE vibecanvas_app LOGIN PASSWORD "
                "'vibecanvas_app' NOSUPERUSER NOBYPASSRLS"
            )
        # CREATE on the DB → can run `CREATE EXTENSION pgcrypto` (a
        # trusted extension); ALL on schema public → can create tables.
        # NOTE: these test grants are deliberately broader than a hardened
        # production role should hold (a prod app role generally should
        # NOT keep CREATE ON DATABASE after migrations). The production
        # deploy task must scope vibecanvas_app's grants down, and this
        # fixture should then be tightened to match.
        conn.execute(f'GRANT CREATE ON DATABASE "{dbname}" TO vibecanvas_app')
        conn.execute("GRANT ALL ON SCHEMA public TO vibecanvas_app")
    # 2. App-role URL — migrations + the app both connect as it.
    app_url = su_url.set(username="vibecanvas_app",
                         password=app_password).render_as_string(
        hide_password=False)
    monkeypatch_session.setenv("DATABASE_URL", app_url)
    _live_config.database.url = app_url
    # 3. Migrate to head AS vibecanvas_app → it owns every table.
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "head")
    # DBOS is part of the product's PostgreSQL contract.  Build its system
    # schema in the same disposable database so transactionally-enqueued
    # domain events (including authorization grants/revocations) are exercised
    # by integration tests instead of being silently mocked out.
    dbos_url = make_url(app_url).set(
        drivername="postgresql+psycopg"
    ).render_as_string(hide_password=False)
    monkeypatch_session.setenv("DBOS_SYSTEM_DATABASE_URL", dbos_url)
    _live_config.dbos_system_database_url = dbos_url
    subprocess.run(
        [
            str(Path(sys.executable).with_name("dbos")),
            "migrate",
            "-s",
            dbos_url,
            "-r",
            "vibecanvas_app",
        ],
        check=True,
        cwd=API_DIR,
        capture_output=True,
        text=True,
    )
    yield


@pytest_asyncio.fixture
async def pg_engine(pg_url, _migrate):
    """Async engine connecting as the SUPERUSER ``postgres`` role — it
    bypasses RLS. For RLS-sensitive tests use ``app_engine`` (the
    non-superuser ``vibecanvas_app`` owner role) instead."""
    eng = create_async_engine(
        pg_url,
        connect_args={"prepared_statement_cache_size": 0},
    )
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def app_engine(_migrate):
    """Async engine connecting as the non-superuser ``vibecanvas_app``
    role — the owner of the business tables, so Postgres RLS + FORCE
    apply (unlike the superuser ``pg_engine``)."""
    eng = create_async_engine(
        _live_config.database.url,
        connect_args={"prepared_statement_cache_size": 0},
    )
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def pg_session(pg_engine):
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as s:
        # The test contract mandates
        # async_sessionmaker(..., expire_on_commit=False) (kept verbatim
        # above, required for repository correctness). The updated-at trigger
        # test commits, then re-reads `updated_at`
        # through the SAME session expecting the value the DB trigger wrote
        # server-side. With expire_on_commit=False the identity-mapped
        # instance is NOT refreshed after commit, so the test reads the
        # stale pre-trigger value (RAW SQL proves the trigger itself is
        # correct — .updated_at does advance in the DB). Registering an
        # after_commit hook that expires loaded instances makes the next
        # attribute access re-fetch the trigger-written value, without
        # altering the mandated sessionmaker kwargs. Same character as the
        # sanctioned _migrate singleton fix: a documented adaptation so the
        # intended verification works.
        #
        # ⚠️ Divergence from production: this test session expires ALL
        # instances after every commit (effectively expire_on_commit=True),
        # which is STRICTER than production `db.py:session_scope`
        # (expire_on_commit=False, no hook). Repository
        # code that reads an ORM attribute *after* `await session.commit()`
        # without an intervening awaited query will raise MissingGreenlet
        # here but NOT in production — re-query/refresh explicitly to be
        # safe. The fixture deliberately avoids transaction reuse across loops.
        @event.listens_for(s.sync_session, "after_commit")
        def _expire_after_commit(sync_sess):
            sync_sess.expire_all()

        yield s
        await s.rollback()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_between_tests(pg_url, _migrate):
    """After each test, wipe all domain tables so tests are isolated.

    Truncate only tables that currently exist so revision-specific test
    databases remain safe while migrations evolve.
    """
    yield
    eng = create_async_engine(
        pg_url,
        connect_args={"prepared_statement_cache_size": 0},
    )
    wanted = [t.strip() for t in _TRUNCATE_TABLES.split(",")]
    try:
        async with eng.begin() as conn:
            rows = await conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ))
            existing = {r[0] for r in rows}
            present = [t for t in wanted if t in existing]
            if present:
                await conn.execute(text(
                    f"TRUNCATE {', '.join(present)} "
                    "RESTART IDENTITY CASCADE"
                ))
    finally:
        await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _isolate_global_engine():
    """db.py's process-global engine binds its asyncpg pool to the event
    loop of the first session_scope()/get_db() call. pytest-asyncio gives
    each test a fresh loop, so disposing before+after each test forces a
    rebuild on the current loop (else: 'RuntimeError: Event loop is
    closed'). Hoisted suite-wide because Task 8/9/10 route tests drive
    db.py's global engine through the ASGI app."""
    await dispose_engine()
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def vfs_run_repo(app_engine, tmp_path):
    """RE-1 — shared by test_vfs_run_repo (T3) and test_vfs_run_release (T4).

    Mirrors test_vfs_store's RLS pattern: seed a real `tenants` row (auth
    table, no RLS), then open an app_engine session bound to that tenant via
    `session_scope(tenant_id=...)` (sets app.tenant_id GUC so the vfs_run
    tenant_id FetchedValue() DEFAULT + FORCE RLS resolve). Bytes go to a real
    FilesystemObjectStore rooted at a tmpdir (NOT InMemory — we want real-file
    behavior, incl. materialize_prefix yielding a real host dir).
    """
    tenant = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                        {"t": tenant})
    store = FilesystemObjectStore(root=str(tmp_path))
    async with session_scope(tenant_id=str(tenant)) as s:
        yield VfsRunRepo(s, store, str(tenant))
        await s.rollback()


class _PermitAllOpenFga:
    """Explicit test control-plane double for non-authorization route tests.

    Authorization integration tests build their own relationship evaluator.
    The general ASGI fixture still supplies the mandatory OpenFGA wire
    contract so production code never needs a test-only fallback.
    """

    def __init__(self) -> None:
        self.tuples: set[OpenFgaTuple] = set()

    async def read(self, *, tuple_key, **_kwargs):
        return OpenFgaReadPage(
            (tuple_key,) if tuple_key in self.tuples else (),
        )

    async def write(self, *, writes=(), deletes=()):
        self.tuples.update(writes)
        self.tuples.difference_update(deletes)

    async def batch_check(self, checks, **_kwargs):
        return tuple(True for _ in checks)

    async def list_objects(self, *, object_type, **_kwargs):
        prefix = f"{object_type}:"
        return tuple(sorted({
            item.object.removeprefix(prefix)
            for item in self.tuples
            if item.object.startswith(prefix)
        }))


@pytest.fixture
def openfga_allow_all():
    return _PermitAllOpenFga()


@pytest_asyncio.fixture
async def client(pg_engine, openfga_allow_all):
    """Unauthenticated httpx client against the ASGI app. Note: the app
    lifespan is NOT run (ASGITransport doesn't trigger it) — the auth /
    CRUD routes only need db.py's lazily-built engine, and the conftest
    `_migrate` fixture has already migrated the schema."""
    app = build_app()
    app.state.openfga_client = openfga_allow_all
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport,
                           base_url="http://testserver") as c:
        yield c
