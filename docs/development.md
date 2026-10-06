# Development Guide

This guide describes the supported source-development workflow for Flowork.
It is intended for contributors working across the workflow engine, API, Web
application, or Browser Extension. For evaluation and day-to-day operation,
follow the [installation guide](installation.md) instead.

## Development model

Flowork is developed as one local stack with four source packages:

| Package | Responsibility | Primary entry points |
| --- | --- | --- |
| [`engine/`](../engine/) | Framework-independent workflow validation and execution | [`workflow.py`](../engine/src/vibecanvas_engine/workflow.py), [`nodes/`](../engine/src/vibecanvas_engine/nodes/) |
| [`api/`](../api/) | HTTP API, Agent Runtime orchestration, persistence, background work, authorization, and sandbox control | [`app.py`](../api/src/vibecanvas_api/app.py), [`routes/`](../api/src/vibecanvas_api/routes/), [`background_worker.py`](../api/src/vibecanvas_api/background_worker.py) |
| [`web/`](../web/) | React application and browser-based product flows | [`src/app/`](../web/src/app/), [`src/pages/`](../web/src/pages/) |
| [`extension/`](../extension/) | Chrome MV3 side panel and controlled-tab browser integration | [`service-worker.ts`](../extension/src/service-worker.ts), [`sidepanel.ts`](../extension/src/sidepanel.ts) |

The API depends on the Engine, while the Web application and Browser Extension
communicate with the API through public contracts. Keep changes within these
boundaries unless an architectural change has been agreed first. The complete
runtime topology is described in the [architecture guide](architecture.md).

## Prepare the environment

Full-stack source development is supported on Debian, Ubuntu, and WSL. Run the
bootstrap as a normal login user; it uses `sudo` for operating-system packages,
global CLI packages, and executable wrappers:

```bash
./scripts/bootstrap_native_linux.sh --prepare-only
```

The bootstrap installs the required host tools, creates the repository-local
Python environment, installs the locked Python and pnpm dependencies, prepares
the bubblewrap backend, and creates `.env.launch.local`. Its implementation
is available in
[`bootstrap_native_linux.sh`](../scripts/bootstrap_native_linux.sh).

The supported toolchain is:

- Python 3.11.16 in `.venv/`;
- Node.js 24.21.0 for native installation, CI, and container builds and pnpm 10.34.4;
- PostgreSQL server binaries, a Redis-compatible local server, and bubblewrap; and
- OpenFGA, started automatically with the local stack.

The native bootstrap installs Flowork's pinned official Codex CLI package; it
does not compile Codex or install a Rust toolchain. Plan peak dependency install,
frontend build, and workload memory separately from steady-state service memory.

For service-only operation, the launcher's default `WEB_MODE=preview` serves a
production Web build. Use `WEB_MODE=dev` only when developing the frontend: its
live compilation and dependency graph have a different memory profile. Agent
sessions, workflow concurrency, and office rendering add workload-dependent
memory on top of the idle services.

Do not install the Python packages into the system interpreter or a Conda
environment. When dependency declarations change, refresh the prepared
environment before continuing:

```bash
./scripts/bootstrap_native_linux.sh --prepare-only
```

Dependency-update policy and lockfile generation are documented in
[`CONTRIBUTING.md`](../CONTRIBUTING.md#dependency-updates).

## Run the local stack

Start all services with the repository launcher:

```bash
WEB_MODE=dev ./launch.sh start
```

This starts PostgreSQL, Redis, and OpenFGA; applies database migrations; and
launches `sandboxd`, the FastAPI application, one DBOS background worker, and
the Vite Web server. It also builds the Browser Extension for
the configured application origin. The application is available at
<http://localhost:9001> and the API at <http://127.0.0.1:8000> by default.

DBOS stores durable queue and schedule state in PostgreSQL. Redis provides
transient coordination and event delivery. Batch and scheduled executions also
use database-backed worker ownership, heartbeats, and recovery checks; this is
part of the background worker, not another daemon. An execution whose worker
is lost is stopped and reported with an unknown outcome rather than automatically
rerunning potentially completed external effects.

The defaults for native, Docker and release deployments are `SANDBOX_RUNTIME=bubblewrap` and
`SANDBOX_TYPE=rootless-warm`. It uses Linux namespace isolation without
checkpoint/restore support. gVisor is available through explicit configuration.
See [Sandbox backend and lifecycle](installation.md#sandbox-backend-and-lifecycle)
before changing these settings; no deployment entrypoint automatically switches
to a snapshot profile.

`WEB_MODE=dev` enables Vite hot module replacement. The launcher's default
`preview` mode builds and serves static assets and is more reliable on hosts
with restrictive file-watcher limits. Python processes do not run with an
automatic reloader; restart the stack after changing Engine or API code.

| Action | Command |
| --- | --- |
| Start | `./launch.sh start` |
| Restart after backend changes | `./launch.sh restart` |
| Stop | `./launch.sh stop` |
| Check service health | `./launch.sh status` |
| Show recent logs | `./launch.sh logs` |

The launcher delegates service orchestration to
[`native_dev_up.sh`](../scripts/native_dev_up.sh). Process state and logs are
stored under `/tmp/vibecanvas-native/` by default; persistent development data
is stored under `~/.vibecanvas/`.

### Web application

The full-stack command above is the normal Web development path. Package-level
checks and a standalone Vite server are also available:

```bash
pnpm --dir web dev
pnpm --dir web test:watch
```

The Vite server proxies `/api` and `/healthz` to the API on port `8000`. Its
runtime and proxy configuration is defined in
[`vite.config.ts`](../web/vite.config.ts).

### Browser Extension

`launch.sh` builds the extension with the same origin used to open Flowork and
publishes a ZIP for the Settings download flow. For extension-only development,
run the watched build:

```bash
pnpm --dir extension dev
```

Load `extension/dist/` as an unpacked extension in Chrome, then reload it from
`chrome://extensions` after a build. When the application
does not use the default `http://localhost:9001` origin, supply the exact public
base and allowlist at build time:

```bash
VITE_WEB_BASE=https://app.example.com \
VITE_EXTENSION_ALLOWED_ORIGINS=https://app.example.com \
pnpm --dir extension build
```

These values are public origins embedded in the extension bundle, not secrets.
The build-time boundary is implemented in
[`extension/vite.config.ts`](../extension/vite.config.ts).

## Validate changes

Run the checks for every package affected by a change. The commands below match
the primary gates in [continuous integration](../.github/workflows/ci.yml).

### Adding or changing a workflow node

A node change includes every entry point that discovers, edits, validates or
executes it. Review these dependencies together:

- Register and export the node in `engine/src/vibecanvas_engine/nodes/` and the
  public engine package. Check the compatibility import and sandbox allowlist.
- Define `CONFIG_SCHEMA` and `AGENT_SPEC`, including valid examples and output
  constraints. CLI `workflow get-spec --list-types` and `get-spec --type TYPE`
  use the canonical registry; do not maintain a separate CLI type list.
- Review the compact BUILD catalog in `agents/prompts/node_definitions.py`, the
  workflow playbook, CLI help and `flowork_cli/AGENTS.md`. Explain execution
  lifetime and limitations where they affect an Agent's choice of command.
- Update the canvas catalog, icon, translations, default configuration and
  configuration editor. Check validation, output references and read-only
  execution details as well as graph editing.
- Exercise canvas full and single-node runs, CLI `run`, `run --node` and
  `run-batch`, batch Tasks, scheduled Tasks and deployments. Shared engine
  support alone does not prove that each host entry point supports the node.
- Propagate any new execution state to persistence, admission/concurrency,
  cancellation, result queries, history filters and CLI exit/result handling.

For `HumanApprovalNode`, verify that waiting retains its worker, each admitted
input has its own `execution_id` and `execution_url`. Human approval/rejection
returns `approved=true/false` and continues. Approval timeout produces no output,
terminates that invocation and its parallel branches, and records `timed_out`
with `approval_timeout`; the approval record has `approved=null`. CLI progress must expose
`row_status=waiting_approval`; unfinished CLI executions remain scoped to the
original Agent turn. Deployment HTTP 202 is acceptance, not execution success.

Run the registry/prompt/CLI contract tests and runtime integration tests, then
deploy the affected services and verify these paths through a real account.
Record execution IDs and expected/actual results. Mocked tests, graph validation
and node discovery do not replace deployed execution acceptance; an unverified
required path remains incomplete.

### Engine and API

```bash
./scripts/sync_python_env.sh
source .venv/bin/activate
python -m pytest -q engine/tests
python -m pytest -q api/tests
ruff check api/src engine/src scripts
python scripts/verify_dependency_locks.py
python -m pip check
```

The synchronization script enforces the repository's `.python-version`
(Python 3.11.16), uv 0.12.19, locked dependencies, and editable installs from
this checkout. Run it after dependency changes or when switching checkouts;
this prevents tests from importing a stale globally installed Flowork package.

Engine tests are self-contained. API tests start an isolated PostgreSQL server
through `pytest-postgresql`; PostgreSQL server binaries, including `pg_ctl`,
must be installed. Set `FLOWORK_TEST_PG_CTL` to an explicit executable when it
is not discoverable in `PATH` or `/usr/lib/postgresql/*/bin/`.

When PostgreSQL intentionally runs in Docker, the API suite can use that
cluster without installing server binaries on the host. Point
`FLOWORK_TEST_PG_URL` at an administrative database, provide the existing
application-role password through `FLOWORK_TEST_APP_PASSWORD`, and optionally
set `FLOWORK_TEST_PG_DATABASE` (default: `vibecanvas_test`). The fixture creates
and later removes only that disposable database. It never migrates, truncates,
or deletes the database named by `FLOWORK_TEST_PG_URL`, and it never alters the
existing `vibecanvas_app` role.

Tests that require a real gVisor environment are guarded and report a skip when
the host kernel cannot provide the required isolation profile. A skipped gVisor
test is not equivalent to release verification; production sandbox gates are
covered by the release process in [`DEPLOY.md`](../DEPLOY.md).

### Web application

```bash
pnpm --dir web lint
pnpm --dir web lint:visual
pnpm --dir web lint:routes
pnpm --dir web lint:locales
pnpm --dir web lint:retired-ui
pnpm --dir web test
pnpm --dir web build
pnpm --dir web lint:bundle
```

### Browser Extension

```bash
pnpm --dir extension test
pnpm --dir extension build
```

### Browser end-to-end tests

Install the Playwright Chromium binary once, start the local stack, and run the
relevant specifications against the existing services:

```bash
pnpm --dir web exec playwright install chromium
./launch.sh start
VIBECANVAS_SKIP_WEB_SERVER=1 \
VIBECANVAS_WEB_PORT=9001 \
VIBECANVAS_PYTHON="$PWD/.venv/bin/python" \
pnpm --dir web exec playwright test e2e/01-login-and-workspace.spec.ts
```

The test runner defaults to its own API and Vite processes when
`VIBECANVAS_SKIP_WEB_SERVER` is not set. The supported options and port mapping
are defined in [`playwright.config.ts`](../web/playwright.config.ts). Replace the
example path with the specifications affected by the change. The full
`pnpm --dir web test:e2e` command also collects real Agent Runtime, Browser
Extension, and acceptance specifications; some require credentials or explicit
environment variables documented in their source files.

## Database changes

The local and Docker launchers apply committed migrations automatically. Schema
changes must include an Alembic revision under
[`api/alembic/versions/`](../api/alembic/versions/) and tests that exercise the
resulting current-head schema. Application startup must not create or mutate
the production schema directly.

Migration commands read `MIGRATION_DATABASE_URL` and fall back to
`DATABASE_URL`, as defined in [`api/alembic/env.py`](../api/alembic/env.py).
Use a dedicated migrator connection rather than the runtime application role:

```bash
cd api
MIGRATION_DATABASE_URL='postgresql+asyncpg://<migrator>@<host>/<database>' \
  alembic revision --autogenerate -m "describe the change"
MIGRATION_DATABASE_URL='postgresql+asyncpg://<migrator>@<host>/<database>' \
  alembic upgrade head
```

Review generated revisions before applying them. Data backfills, encryption
cutovers, and other operational migrations may require the dedicated scripts
used by the launchers and must not be reduced to an unreviewed Alembic command.

## Generated contracts

Generated files are committed so Web builds do not depend on a running backend
or local generator. Regenerate the relevant contract in the same change as its
source.

After changing the public API schema, start the API and refresh both the OpenAPI
snapshot and TypeScript definitions:

```bash
pnpm --dir web codegen:snapshot
```

After changing the backend interactive-view schema, refresh the frontend
validator:

```bash
.venv/bin/python scripts/generate_interactive_view_contract.py
```

The sandbox gRPC bindings
[`sandbox_service_pb2.py`](../api/src/vibecanvas_api/services/sandbox/proto/sandbox_service_pb2.py)
and
[`sandbox_service_pb2_grpc.py`](../api/src/vibecanvas_api/services/sandbox/proto/sandbox_service_pb2_grpc.py)
are generated from
[`sandbox_service.proto`](../api/src/vibecanvas_api/services/sandbox/proto/sandbox_service.proto).
Do not edit the generated Python files by hand; regenerate and review both
bindings whenever the service definition changes.

## Before opening a pull request

Keep a change focused, add tests for changed behavior, update public
documentation when contracts or configuration change, and run the affected
package checks. The full contribution policy, commit conventions, and pull
request expectations are defined in [`CONTRIBUTING.md`](../CONTRIBUTING.md).

Security-sensitive findings must follow [`SECURITY.md`](../SECURITY.md) rather
than a public issue.
