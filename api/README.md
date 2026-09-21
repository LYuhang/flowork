# Flowork API

The API package provides Flowork's HTTP interface and coordinates the backend
services behind Chat, Workflow, Task, Browser, and Deployment experiences. It
connects product requests to the Agent Runtime, workflow engine, persistent
state, authorization, background jobs, and isolated execution environments.

The installable package and Python import namespace remain `vibecanvas-api` and
`vibecanvas_api` for compatibility.

For an end-user overview, begin with the repository [README](../README.md). This
document is intended for contributors working on the backend package.

## Responsibilities and boundaries

The API package owns:

- versioned HTTP contracts, authentication, request validation, and streaming;
- Agent Runtime orchestration and model/MCP brokering;
- application persistence, database migrations, and object-store integration;
- tenant authorization through OpenFGA;
- durable and scheduled work through DBOS and PostgreSQL; and
- the control contracts used to request isolated execution from `sandboxd`.

Framework-independent workflow definitions and execution primitives belong to
the sibling [`engine/`](../engine/) package. Browser-facing product behavior
belongs to [`web/`](../web/), while privileged sandbox lifecycle management is
implemented by the dedicated sandbox service. See the
[architecture guide](../docs/architecture.md) for the complete system design.

## Runtime topology

The backend is deployed as a group of cooperating processes rather than as one
standalone web server:

| Component | Role |
| --- | --- |
| **API** | Serves HTTP and streaming requests, validates access, coordinates Agent Runtime operations, and submits durable work |
| **DBOS background worker** | Executes queued interactive, deployment, knowledge-indexing, maintenance, and periodic jobs |
| **`sandboxd`** | Owns gVisor processes and isolated execution lifecycle; API and worker processes communicate with it through the sandbox service contract |
| **PostgreSQL** | Stores application state, authorization projections, execution records, and Agent Runtime checkpoints |
| **Redis-compatible service** | Provides transient event fanout, counters, rate limits, and locks; it is not the task broker |
| **OpenFGA** | Evaluates tenant and resource authorization from the pinned model |
| **Object store** | Stores encrypted file and content payloads shared across backend processes |

The supported local launchers create and connect these components. Starting
only the FastAPI process is useful for focused backend development, but it does
not replace the full service topology.

## Development

Prepare the repository environment and start the complete source-development
stack from the repository root:

```bash
./scripts/bootstrap_native_linux.sh --prepare-only
WEB_MODE=dev ./launch.sh start
```

The API is available at <http://127.0.0.1:8000> by default. Python processes do
not automatically reload when source files change; use `./launch.sh restart`
after backend changes.

The [development guide](../docs/development.md) documents prerequisites,
package checks, logs, database changes, and generated contracts. The
[installation guide](../docs/installation.md) covers evaluation and local
self-hosting instead.

### Run the API process directly

After the repository environment and dependent services have been prepared,
the package CLI can run only the HTTP process:

```bash
source .venv/bin/activate
vibecanvas-api serve --host 127.0.0.1 --port 8000 --reload
```

This command expects the database, Redis-compatible service, OpenFGA, sandbox
service, and required environment configuration to be available already. The
canonical configuration template is [`.env.example`](../.env.example); use the
repository launchers to assemble a consistent local configuration.

## HTTP interface

Most application endpoints are grouped under `/api/v1`; protocol-specific
interfaces such as SCIM retain their standard paths. The route modules in
[`routes/`](src/vibecanvas_api/routes/) define the public contracts for
authentication, organizations, Chat, Workflow, Task, Deployment, Browser,
skills, knowledge, files, and runtime services.

Local development exposes the following discovery endpoints:

- `GET /healthz` — process liveness;
- `GET /docs` — Swagger UI;
- `GET /redoc` — ReDoc; and
- `GET /openapi.json` — the generated OpenAPI document.

Interactive API documentation and the OpenAPI endpoint are disabled by the
production security profile. The CLI can also generate a schema without
starting Uvicorn:

```bash
vibecanvas-api dump-openapi --output /tmp/flowork-openapi.json
```

When a public contract changes, update the committed Web snapshot and
TypeScript definitions using the workflow in
[Generated contracts](../docs/development.md#generated-contracts).

## Persistence and migrations

SQLAlchemy repositories and database models live under
[`storage/`](src/vibecanvas_api/storage/). Alembic revisions are committed in
[`alembic/versions/`](alembic/versions/), and the local launchers apply them
before application processes start.

For a deliberate package-level migration against a prepared database, use a
dedicated migrator connection:

```bash
cd api
MIGRATION_DATABASE_URL='postgresql+asyncpg://<migrator>@<host>/<database>' \
  alembic upgrade head
dbos migrate \
  -s 'postgresql+psycopg://<migrator>@<host>/<database>' \
  -r vibecanvas_app
```

Production API and worker processes do not receive schema-changing authority;
migrations run as a separate deployment workload. Data backfills and encryption
cutovers may require additional operational scripts, so do not replace the
documented deployment sequence with an unreviewed Alembic command.

## Background work and sandbox execution

[`services/background_queue.py`](src/vibecanvas_api/services/background_queue.py)
defines the runtime-neutral submission boundary. DBOS workflow/schedule
registrations live in
[`background_workflows.py`](src/vibecanvas_api/background_workflows.py), while
business implementations live under
[`background_tasks/`](src/vibecanvas_api/background_tasks/). One background
worker process handles asynchronous deployments, scheduled runs, knowledge
indexing, reconciliation, and maintenance.

Sandbox contracts and clients live under
[`services/sandbox/`](src/vibecanvas_api/services/sandbox/). In the supported
topology, only `sandboxd` starts and owns gVisor processes; the API and background
worker request execution through its Unix-socket or mTLS gRPC interface. The
[sandbox lifecycle](../docs/architecture.md#sandbox-lifecycle) section explains
the isolation and ownership boundary in detail.

## Tests

Run the API suite from the repository root:

```bash
source .venv/bin/activate
python -m pytest -q api/tests
```

The suite starts isolated PostgreSQL instances through `pytest-postgresql`, so
PostgreSQL server binaries such as `pg_ctl` must be installed. Tests requiring
gVisor or other system capabilities skip when their prerequisites are absent;
a skip is not equivalent to release verification. For targeted commands and
the full validation matrix, see
[Validate changes](../docs/development.md#validate-changes).

## Container image

Use the repository root as the build context because the image includes both
the API and Engine packages:

```bash
docker build -f api/Dockerfile -t flowork-api:dev .
```

The image is shared by the API, background worker, migration, and sandbox service roles in
the Compose topology; it is not a complete deployment by itself. Use the
[installation guide](../docs/installation.md) for a local stack and
[`DEPLOY.md`](../DEPLOY.md) for production requirements.

## Source map

| Path | Responsibility |
| --- | --- |
| [`app.py`](src/vibecanvas_api/app.py) | FastAPI factory, lifespan, middleware, and route registration |
| [`routes/`](src/vibecanvas_api/routes/) | HTTP and streaming endpoints |
| [`schemas/`](src/vibecanvas_api/schemas/) | Public request and response models |
| [`agents/`](src/vibecanvas_api/agents/) | Agent prompts, commands, middleware, and tools |
| [`services/agent_runtime/`](src/vibecanvas_api/services/agent_runtime/) | Agent Runtime adapters and lifecycle |
| [`streaming/`](src/vibecanvas_api/streaming/) | Turn buffering and server-sent event delivery |
| [`storage/`](src/vibecanvas_api/storage/) | PostgreSQL repositories and persistence models |
| [`authorization/`](src/vibecanvas_api/authorization/) | Authorization manifest, OpenFGA model, and adapters |
| [`background_tasks/`](src/vibecanvas_api/background_tasks/) | Runtime-neutral asynchronous and scheduled job implementations |
| [`services/sandbox/`](src/vibecanvas_api/services/sandbox/) | Sandbox service contracts, clients, and lifecycle implementation |
| [`security/`](src/vibecanvas_api/security/) | Production security validation, cryptography, and security controls |
| [`alembic/`](alembic/) | Database migration environment and revisions |

## License

Apache-2.0. See [`LICENSE`](../LICENSE).
