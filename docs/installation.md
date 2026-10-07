# Installation

Flowork supports two local installation methods. Docker Compose is recommended
for evaluation and self-hosted use. The native Linux setup is intended for
contributors who need to work directly with the source code.

For image builds, deployment without a source checkout, HTTPS and public URL
configuration, see [Docker image deployment](docker-deployment.zh-CN.md).

| Method | Recommended for | Host requirements |
| --- | --- | --- |
| **Docker Compose** | Evaluation and local self-hosting | Docker Engine or Docker Desktop with Compose v2 |
| **Native Linux / WSL** | Development and debugging | Debian, Ubuntu, or WSL with `apt`, `sudo`, and network access |

Production deployments have different security and release requirements. Do
not expose the local stack directly to the Internet; use the
[production deployment guide](../DEPLOY.md) instead.

The Compose stack builds separate application PostgreSQL 15 and OpenFGA
PostgreSQL 17 images. Valkey and the optional ClamAV overlay also build patched images.
These images apply distribution security updates during the build;
their existing data volumes and major versions remain independent. Attested
production deployments also require `VIBECANVAS_OPENFGA_POSTGRES_IMAGE` and `VIBECANVAS_VALKEY_IMAGE`; see
[the production release procedure](../DEPLOY.md).

## Docker Compose

### Prerequisites

Install the following tools before starting:

- Git;
- Docker Engine or Docker Desktop;
- Docker Compose v2;
- OpenSSL and `curl`.

### System requirements

The supported requirements apply to the complete stack, including Agent
sandboxes, Office document conversion, diagram export, databases, and
background workers.

| Resource | Minimum | Recommended |
| --- | ---: | ---: |
| CPU available to Docker | 2 vCPU | 4 vCPU |
| Memory available to Docker | 2 GiB plus swap for small deployments | 4 GiB or more |
| Free disk before installation | 10 GiB plus data/build-cache growth | 20 GiB or more |

Docker Desktop users must allocate these CPU and memory resources to the Linux
engine; host totals alone are not sufficient. Disk capacity must cover the
repository, images and build cache, database and object-store growth, and
sandbox data. These numbers target low concurrency: two resident bubblewrap
sandboxes and one DBOS worker process (two executor threads, concurrency one
per queue). They are not a guarantee for concurrent Agent/Office/browser jobs.
Build images sequentially on a small VPS. A GPU is not required.

The preflight checks the Docker allocation and available repository filesystem
space before a build starts. A deliberately undersized test installation can
set `FLOWORK_ALLOW_UNSUPPORTED_RESOURCES=true` for that invocation, but such a
deployment is outside the supported configuration and may fail when an Agent,
LibreOffice, or diagram renderer runs. The host must also support privileged
Linux containers and the selected sandbox startup probe; compatibility is verified
automatically before the API begins accepting traffic.

Docker installation does not require Python, Node.js, or pnpm on the host. All
application dependencies are built into the project images.

The application database image is built from `postgres/Dockerfile`: PostgreSQL
15 on Debian bookworm with pgvector 0.8.6, preserving the existing data-volume
path and initialization hooks. Building it independently allows OS security
updates without waiting for an upstream pgvector image rebuild. Production
releases publish and verify this image alongside API, sandbox service, and Web
images; see the [production deployment guide](../DEPLOY.md).

#### Fresh Ubuntu server

On a fresh Ubuntu host, install Docker Engine and Compose from Docker's official
APT repository. Follow the current [Docker Engine installation guide](https://docs.docker.com/engine/install/ubuntu/),
including its repository-signing and package-installation steps. Ensure the
login user can run `docker info` and `docker compose version` without `sudo`
before starting Flowork; after adding the user to the `docker` group, sign out
and start a new SSH session so the membership takes effect.

For Ubuntu 24.04 (Noble) on amd64, the repository also provides
[`install_docker_ubuntu.sh`](../scripts/install_docker_ubuntu.sh). After cloning
Flowork, run the script as the normal login user; it configures Docker's signed
APT repository, installs Engine, Buildx, and Compose, and verifies a rootful
daemon. Start a new login session before continuing with the commands below.

For a long first build over SSH, use a persistent terminal such as `tmux` or
`screen`, or arrange for the command to continue after a connection loss. The
launcher is idempotent and can be run again if an interrupted client did not
leave a build process running.

### Install and start

Clone the repository and start the stack:

```bash
git clone https://github.com/LYuhang/flowork.git
cd flowork
./scripts/deploy/local_server.sh up
```

On the first run, the launcher:

1. creates `.env` from `.env.example`;
2. generates independent local passwords and encryption keys;
3. restricts `.env` permissions to `0600`;
4. validates Docker, Compose, the network binding, and required settings;
5. builds and starts the services;
6. applies database migrations and initializes OpenFGA; and
7. verifies the Web, API, `sandboxd`, and the selected sandbox lifecycle.

The first build installs the pinned official Codex binary without compiling
Rust. Office/diagram tools, Python dependencies and frontend assets still take
time and build-cache space.
When verification succeeds, open
<http://localhost:9001>.

The supported entry point is
[`local_server.sh`](../scripts/deploy/local_server.sh). It operates the service
topology defined in [`docker-compose.yml`](../docker-compose.yml); individual
Dockerfiles are build inputs, not separate installation procedures.

### Configure before the first start

The default command is sufficient for a localhost installation. To review or
change settings before any service starts, initialize the environment file
separately:

```bash
./scripts/deploy/local_server.sh init
```

Edit `.env`, then start the stack:

```bash
./scripts/deploy/local_server.sh up
```

If `.env` already exists, the initializer keeps configured secrets, fills missing
settings, and normalizes the supported Agent Runtime selection. Do not delete
or regenerate an environment file after storing data: it contains encryption
keys required to read the existing object store.

### Manage the stack

| Action | Command |
| --- | --- |
| Start or apply local changes | `./scripts/deploy/local_server.sh up` |
| Stop and remove containers | `./scripts/deploy/local_server.sh stop` |
| Restart containers | `./scripts/deploy/local_server.sh restart` |
| Show service status | `./scripts/deploy/local_server.sh status` |
| Follow all logs | `./scripts/deploy/local_server.sh logs` |
| Follow one service | `./scripts/deploy/local_server.sh logs api` |
| Run deployment verification | `./scripts/deploy/local_server.sh verify` |
| Run prerequisites only | `./scripts/deploy/local_server.sh preflight` |

Stopping the stack does not remove its named volumes. The database, object
store, runtime files, and generated `.env` remain available for the next start.

`preflight` validates the resolved Compose file without printing its values.
The separate `config` command prints the full configuration, including secrets;
do not share that output in logs or issue reports.

### Docker Desktop and WSL2

When Docker Desktop provides the daemon through WSL2 integration, use that
daemon directly. Do not install a second Docker daemon inside the WSL
distribution: the two daemons use different sockets, images, and volumes.

It is normal for `systemctl is-active docker` to report an inactive service in
this configuration. `docker info` must still succeed from the shell where
Flowork is started. The startup probe determines whether the Docker Desktop
kernel supports the selected sandbox backend (bubblewrap by default).

## Native Linux or WSL

The native installer supports Debian, Ubuntu, and WSL distributions that use
`apt`. Run it as a normal login user. The script invokes `sudo` to install system
packages, global CLI packages, and executable wrappers; do not run the entire
script as root.

### Install and start

Clone the repository, then run the bootstrap:

```bash
git clone https://github.com/LYuhang/flowork.git
cd flowork
./scripts/bootstrap_native_linux.sh
```

The bootstrap installs the required system packages, Node.js, pnpm, Codex CLI,
uv, Python dependencies, frontend dependencies, bubblewrap for the default
native sandbox backend (or pinned gVisor when explicitly selected),
headless LibreOffice, Poppler, and the checksum-verified draw.io Desktop CLI.
LibreOffice renders Word and PowerPoint files and provides headless office-file
conversion and validation for document-generation workflows; native XLSX files
are displayed by the browser workbook renderer. Poppler renders PDF pages. The
draw.io CLI and its disposable Xvfb display produce the image feedback used to
review native diagrams. The
installer verifies these runtime commands before creating the local
configuration and starting Flowork through a delegated systemd service.
A running systemd host with cgroup v2 is required for resident deployments.
The installer does not restart an already-running service.

The bootstrap installs the official, version-pinned Codex CLI 0.157.1 package
and verifies its reported version before starting services. Flowork no longer
compiles a patched Codex executable from source, so a fresh installation does
not require a Rust toolchain or a large compilation cache. Container builds copy
the same official architecture-specific native bundle from the npm package.
`CODEX_CLI_PATH` remains an explicit operator override for controlled diagnostic
runtimes, but startup still requires the configured executable to report the
same pinned version.

To prepare the environment without starting services:

```bash
./scripts/bootstrap_native_linux.sh --prepare-only
```

Start the prepared installation later with:

```bash
sudo .venv/bin/python scripts/install_native_service.py --user "$(id -un)"
sudo systemctl start flowork.service
```

If dependencies are installed manually instead of through the bootstrap, add
the server-oriented office packages before starting Flowork:

```bash
sudo apt-get update
sudo apt-get install -y \
  xvfb xauth \
  libreoffice-writer-nogui \
  libreoffice-impress-nogui \
  libreoffice-calc-nogui \
  poppler-utils ffmpeg
```

These are non-GUI packages; a desktop LibreOffice installation is not
required. Native Diagram feedback additionally requires the pinned draw.io
Desktop package and `flowork-drawio-export` wrapper installed by
[`bootstrap_native_linux.sh`](../scripts/bootstrap_native_linux.sh). For a
manual installation, follow that script's architecture selection, SHA-256
verification, package-metadata checks, and wrapper installation rather than
downloading an unverified latest package.

FFmpeg and FFprobe let the Agent generate and inspect real MP3, MP4 and WebM
files. They are installed by the native bootstrap and the API runtime image.
Browser audio/video previews still use the browser's supported codecs; FFmpeg
is a generation dependency, not a server-side playback or transcoding service.

Verify that the commands used by the document and diagram runtimes are
available:

```bash
libreoffice --version  # `soffice --version` is also accepted
pdftoppm -v
ffmpeg -version
ffprobe -version
command -v drawio flowork-drawio-export
```

The implementation is available in
[`bootstrap_native_linux.sh`](../scripts/bootstrap_native_linux.sh) and
[`launch.sh`](../launch.sh).

### Local files

Native installation keeps its dependencies and configuration outside the
system Python environment:

| Path | Purpose |
| --- | --- |
| `.venv/` | Repository-local Python environment managed by uv |
| `.env.launch.local` | Local listener and feature configuration; mode `0600` |
| `~/.vibecanvas/` | Persistent local secrets and runtime data |
| `/tmp/vibecanvas-native/` | Process identifiers and logs by default |

These repository-local files are ignored by Git. Do not point
`VIBECANVAS_PYTHON` to Conda or a system interpreter; the launcher expects the
environment created under this checkout.

`launch.sh` uses this checkout's uv `.venv` directly for the API, background
worker and sandbox service. Sandboxes mount the selected Python environment
read-only; no second Python environment is copied or independently maintained.

Set `VIBECANVAS_NATIVE_RUNTIME_DIR` before running `launch.sh` if process state
and logs should be stored somewhere other than `/tmp/vibecanvas-native`.

### Deployment-specific native configuration

Scripts derive the checkout from their own location; they do not assume a
particular user's checkout path. Supply machine-specific settings when invoking
the scripts, or in a protected shell configuration selected with
`VIBECANVAS_LAUNCH_ENV` (default: this checkout's `.env.launch.local`). The
bootstrap and launcher use the same configuration path. Existing configuration
is preserved; explicit assignments in that file override inherited variables.
The generated Web defaults honor inherited overrides.

| Setting | Purpose |
| --- | --- |
| `WEB_HOST`, `WEB_PORT`, `VIBECANVAS_PUBLIC_URL` | Listener and actual browser-facing URL |
| `API_PORT` | Loopback API listener and native Web proxy target (default `8000`) |
| `PGPORT`, `REDISPORT` | Native database and queue ports (defaults: `5433`, `6380`; separate from system services) |
| `OPENFGA_HTTP_PORT`, `OPENFGA_GRPC_PORT`, `OPENFGA_METRICS_PORT` | Authorization listeners |
| `PGDATA`, `OBJSTORE` | Persistent database and object-store locations |
| `AGENT_RUNTIME_ROOT`, `VFS_VOLUME_ROOT`, `VIBECANVAS_STORAGE_ROOT` | Persistent application/runtime storage |
| `VIBECANVAS_LOCAL_SECRET_DIR` | Local encryption-key directory |
| `VIBECANVAS_NATIVE_RUNTIME_DIR` | Per-instance process/log directory |

When running more than one instance, choose distinct ports **and** distinct
data, secret, and runtime directories. Do not reuse another instance's
PID files or databases. Keep all local configuration and credentials outside
version control. `status` returns a nonzero exit code if a required service is
unhealthy; a successful install alone does not mean the services have started.

### Manage the native stack

| Action | Command |
| --- | --- |
| Start | `sudo systemctl start flowork.service` |
| Stop | `sudo systemctl stop flowork.service` |
| Restart | `sudo systemctl restart flowork.service` |
| Show status | `./launch.sh status` |
| Show recent logs | `./launch.sh logs` |

The native launcher defaults to `WEB_REBUILD=auto`. Set `WEB_REBUILD=0` when
publishing a verified prebuilt `web/dist`; it reuses that artifact even when
source timestamps are newer, but still builds if `dist/index.html` is missing.
Publish build files as the service user, or use `rsync --chown=flowork:flowork`
(substitute your service account) when copying as root. Both `web/dist` and the
runtime `web-dist` must remain writable by that user for subsequent updates.
Copy hashed assets before replacing `index.html` so ongoing page loads can
finish using the previous build.

For manual dependency installation, test commands, and frontend development,
continue with the [development guide](development.md).

### Sandbox backend and lifecycle

`SANDBOX_RUNTIME` selects the execution backend; `SANDBOX_TYPE` selects its
privilege and lifecycle profile. Native, Docker, release and image-only
deployments all default to `bubblewrap` with `rootless-warm`. The release
overlay preserves these defaults and any explicit operator selection.
Checkpoint/restore belongs to the rootful gVisor snapshot profile; a warm native
session does not imply that its process state can be checkpointed.

The native bootstrap installs
`bwrap`, and startup probes whether the actual host can run it. This backend
supports resident workers and Agent/Workflow execution, but has no real
checkpoint/restore or post-start dynamic mount support. It shares the host
kernel and the current implementation binds the host `/proc` read-only, so its
isolation differs from the optional gVisor backend. See
[Sandbox lifecycle](architecture.md#sandbox-lifecycle) for the capability boundary.

Installing `runsc` is not proof that the host supports the full sandbox profile.
Native deployments may explicitly select `SANDBOX_RUNTIME=gvisor`; the
bootstrap then also installs the pinned `runsc` executable.
Nested cloud containers and some WSL kernels can reject gVisor even when
ordinary user namespaces work. Startup must pass the actual sandbox prewarm
check. On an unsuitable host, choose a supported host or explicitly select the
reduced-capability native bubblewrap backend; the installer does not silently
change the isolation backend to make a health check pass.

## First-run setup

After the stack is healthy:

1. open <http://localhost:9001>;
2. create a user account or sign in;
3. open **Settings → Agent Runtime**;
4. open **Settings → API credentials** to connect OpenRouter or add a provider
   key, or connect a Codex account under **Settings → Agent Runtime**; and
5. start a Chat or create a Workflow.

A model provider is not required for the platform to start, but Agent Chat
cannot run until a compatible model connection is available. User-managed
providers can be configured from Settings; a deployment-wide default is
optional. Settings controls the available Codex account/API sources. A new Chat
selects its concrete source, model, and
model-supported thinking level in the composer. The first accepted turn fixes
the exact account/API connection for that Chat; subsequent turns may change
the model and thinking level only within that connection.
OpenRouter uses `VIBECANVAS_PUBLIC_URL` as its fixed OAuth callback origin, so
that value must match the address users open in the browser. A connected
OpenRouter account supplies compatible text and tool-calling models to the
Codex Runtime. Codex uses the Responses API through Flowork's host-side
model broker. The catalog can
be refreshed from Settings without changing existing Chat history. Model
catalog visibility does not imply invocation entitlement: account credits,
free-model daily quotas, and provider availability are enforced by OpenRouter
when a Turn runs. See OpenRouter's [rate-limit FAQ](https://openrouter.ai/docs/faq).
Direct outbound access is the default; deployments that require an HTTP(S)
proxy for host-side provider traffic can set
`FLOWORK_CONTROL_PLANE_HTTP_PROXY`. Leave it
empty on ordinary servers—Flowork never infers this value from a developer's
desktop `HTTP_PROXY` environment.

### Install the Browser Extension

Each deployment builds an extension package for its configured public URL. In
Flowork, open **Settings → Extensions → Download extension**, then:

1. extract the ZIP to a permanent folder;
2. open `chrome://extensions`;
3. enable **Developer mode**;
4. choose **Load unpacked** and select the extracted folder; and
5. pin Flowork and open its side panel.

If the deployment URL changes, rebuild or restart the deployment and download
the extension again. The allowed Web origin is compiled into the extension
package.

## Common configuration

Docker reads `.env`; native installation reads `.env.launch.local`. The
following runtime settings are occasionally changed independently:

| Variable | Local default | Purpose |
| --- | --- | --- |
| `VIBECANVAS_HTTP_PORT` | `9001` | Docker Web application port |
| `WEB_PORT` | `9001` | Native launcher Web application port |
| `SANDBOX_RUNTIME` | `bubblewrap` (all deployment entrypoints) | Sandbox backend |
| `SANDBOX_TYPE` | `rootless-warm` (all deployment entrypoints) | Sandbox privilege and lifecycle profile |
| `SANDBOX_MAX_RESIDENT` | `2` | Caps concurrently warm Agent/Workflow sandboxes; raise only after sizing per-session memory |
| `OBJECT_STORE_PROVIDER` | `filesystem` | Local file-backed object storage; production deployments normally use `s3` |
| `SANDBOX_EGRESS_MODE` | `proxy` | Routes sandbox HTTP(S) and WebSocket traffic through the controlled egress proxy |
| `SANDBOX_EGRESS_POLICY` | `public` | Controls whether sandboxes may reach public destinations, an allowlist, or platform services only |
| `FLOWORK_CONTROL_PLANE_HTTP_PROXY` | (empty string) | Optional HTTP(S) proxy for host-side provider OAuth, catalogs, and metadata; unrelated to sandbox egress |
| `MOUNT_PATH` | (empty string) | Optional trusted host directory exposed through each user's isolated `/mount` path |

Keep `VIBECANVAS_INTERNAL_BIND_ADDRESS` at its `127.0.0.1` default. It protects
API diagnostics, databases, authorization services, queues, and metrics from
being published with the Web entry point. The full advanced template is
documented inline in [`.env.example`](../.env.example). Restart the affected
services after changing configuration. `VIBECANVAS_INTERNAL_BIND_ADDRESS` is a
Compose setting; native services use their launcher-specific listener settings.

When changing the Docker Web port, edit `VIBECANVAS_HTTP_PORT` in `.env`, then
run `./scripts/deploy/local_server.sh up --public-url http://localhost:<port>`
with the actual port. Use `--public-url` for a changed public origin as well:
it updates the related host allowlist, CORS, cookie, and extension build settings.
Editing only `VIBECANVAS_PUBLIC_URL` does not refresh previously generated values.

Keep generated passwords, signing keys, browser token secrets, and encryption
keys out of Git, shell history, logs, and issue reports. Back up secret material
separately from persistent data. Production secret management, storage, and
egress requirements are covered in [Production deployment](../DEPLOY.md).

For the design behind the sandbox modes and network controls, see
[Sandbox lifecycle](architecture.md#sandbox-lifecycle) and
[Network boundaries](architecture.md#network-boundaries).

## Updating a source installation

Flowork is currently in alpha. Review release notes and take a consistent backup
before updating. Protect both authoritative data and the keys needed to read it:

| State | Docker Compose | Native installation |
| --- | --- | --- |
| Application database, DBOS tasks and schedules | PostgreSQL persistent volume | `PGDATA` (default `~/.vibecanvas/pgdata`) |
| Authorization database | OpenFGA PostgreSQL persistent volume | OpenFGA database in the same native PostgreSQL cluster |
| Encrypted objects | Object-store volume or configured S3 bucket | `OBJSTORE` (default `~/.vibecanvas/objectstore`) |
| Chat/runtime and VFS data | Runtime/VFS persistent volumes | `AGENT_RUNTIME_ROOT` and `VFS_VOLUME_ROOT` under `~/.vibecanvas/` by default |
| Configuration and encryption keys | Protected `.env` and any external secret/key provider | `.env.launch.local` **and** local key files |

Native keys default to `~/.vibecanvas/secrets/kms-master.key` and
`~/.vibecanvas/secrets/content-lookup-hmac.key`. Back up the actual paths selected
by `KMS_LOCAL_MASTER_KEY_FILE`, `CONTENT_LOOKUP_HMAC_KEY_FILE`, or
`VIBECANVAS_LOCAL_SECRET_DIR` when overridden. Keep key backups separately
protected, and never regenerate them to repair an existing installation.
An environment-file-only backup is insufficient for native encrypted data.
Use PostgreSQL backup tools or a clean shutdown; copying a live data directory
is not a consistent database backup. Test restoration before relying on it.

Schedule a maintenance window: stop new submissions at your ingress, pause
scheduled tasks, and wait for active Chat turns, Workflow runs, and queued/running
background tasks to finish or cancel them explicitly. Neither launcher performs
an application-level drain. `launch.sh start` also stops an existing native
stack; Docker's `--wait` checks service health, not completion of user work.
After restart, inspect interrupted tasks and external side effects before
retrying them, then resume schedules and traffic.

Stop a native installation with its existing launcher before replacing that
launcher during an upgrade. New launchers refuse to signal processes whose PID
files do not carry their instance ownership marker; they never kill unrelated
processes by a shared service name. If upgrading from an older launcher, do not
delete live PID files to bypass this check—verify and stop the old processes first.

For Docker Compose:

```bash
git pull --ff-only
./scripts/deploy/local_server.sh up
```

The launcher rebuilds changed images, applies migrations, and verifies the
resulting stack.

For a native installation:

```bash
sudo systemctl stop flowork.service
git pull --ff-only
./scripts/bootstrap_native_linux.sh --prepare-only
sudo .venv/bin/python scripts/install_native_service.py --user "$(id -un)"
sudo systemctl start flowork.service
```

Production systems must use verified release images and the upgrade procedure
in [Production deployment](../DEPLOY.md), not a source checkout update.

## Troubleshooting

### Start with status and logs

For Docker Compose:

```bash
./scripts/deploy/local_server.sh status
./scripts/deploy/local_server.sh logs api
./scripts/deploy/local_server.sh logs sandboxd
```

For a native installation:

```bash
./launch.sh status
./launch.sh logs
```

### Run the Docker checks separately

Use the preflight command to check Docker, Compose, generated secrets, network
binding, and the resolved Compose file without starting services:

```bash
./scripts/deploy/local_server.sh preflight
```

Use the verifier to check health endpoints, the private `sandboxd` socket, and
the selected sandbox lifecycle (checkpoint/restore only in snapshot mode):

```bash
./scripts/deploy/local_server.sh verify
```

The verification steps are implemented in
[`preflight.sh`](../scripts/deploy/preflight.sh) and
[`verify_local.sh`](../scripts/deploy/verify_local.sh).

### Common failures

| Symptom | Recommended action |
| --- | --- |
| `Docker daemon is unavailable` | Run `docker info`. Start Docker Engine or enable Docker Desktop integration for the current WSL distribution. |
| `Docker Compose v2 plugin is unavailable` | Install or update the Compose v2 plugin; the legacy `docker-compose` command is not supported. |
| A Docker service port is already in use | Change the corresponding `VIBECANVAS_*_PORT` value in `.env`. For a new Web port, run `local_server.sh up --public-url` with the matching URL as described above. Native Web uses `WEB_PORT` in `.env.launch.local`. |
| `sandbox_prewarm` or checkpoint/restore fails | Inspect the `sandboxd` logs. Confirm that the active Docker kernel permits the selected sandbox backend and nested namespaces. Check checkpoint/restore only if gVisor snapshots were explicitly enabled. |
| Native startup reports a missing `.venv` | Run `./scripts/bootstrap_native_linux.sh --prepare-only`, then retry. |
| The application opens but Chat cannot start | Configure a model under **Settings → Agent Runtime**, then inspect the API logs for provider or credential errors. |
| The extension cannot connect | Download the package from the current deployment again and confirm that `VIBECANVAS_PUBLIC_URL` matches the URL opened in Chrome. |
| Account deletion reports an OpenFGA erasure configuration error | For custom infrastructure, provision the scoped OpenFGA change-feed erasure function and set `OPENFGA_ERASURE_DATABASE_URL`. Compose and the native launcher configure it automatically. See [Security and data lifecycle](security-and-data-lifecycle.md#account-deletion). |

Do not resolve an encryption or authentication error by replacing `.env` or
`.env.launch.local`; doing so can make existing encrypted data inaccessible.

## Next steps

- Learn how the services fit together in [Architecture](architecture.md).
- Prepare a hardened deployment with [Production deployment](../DEPLOY.md).
- Set up tests and development tools with the [Development guide](development.md).
- Review data protection and retention in
  [Security and data lifecycle](security-and-data-lifecycle.md).

### SubAgent Skills and MCP runtime

SubAgent resources use the API runtime dependencies already installed by the
native bootstrap and API image: the locked requirements include `mcp==1.28.1`,
`langchain-core==1.5.3` and `jsonschema==4.26.0`. No additional background service
or Docker container is required. Rebuild the API image after upgrading source;
copying only frontend assets does not update the Workflow resource broker or CLI.
Database migration 145 adds explicit Skill/MCP grants for task and deployment
service accounts. Use the normal migration and OpenFGA bootstrap on upgrade.

For a custom **stdio** MCP, install its executable and dependencies in the
sandbox runtime environment and configure a path available there. A path from
a developer's laptop is not portable to the Linux host or container. For remote
HTTP/SSE MCPs, configure the server URL and credentials in the MCP resource;
ensure the sandbox egress policy permits the destination. Do not place secrets
in Workflow JSON, prompts or Docker build arguments.

In the node editor select installed resources by ID. Publish Skill changes to
make them available to the next execution; a draft is not executable content.
Publishing a Skill does not require rebuilding an image or restarting a
resident deployment. The backend updates the existing read-only `/skills`
view and preserves each active execution's content snapshot. Task/deployment
resource grants are separate from the creator's interactive Chat session.

See [SubAgent Workflow resources](architecture.md#subagent-workflow-resources)
for connection lifetime, cancellation and permission-revocation behavior.

### Browser runtime installation and diagnostics

The native bootstrap installs `flowork-browser-runtime` 0.4.0 and its pinned
`playwright-core` 1.63.0-alpha-2026-08-05 dependency as a self-contained global
npm package. It packs the source first; installing the source directory directly
with `npm install -g <directory>` creates a symlink and can break browser control
when the checkout's ignored `node_modules` is cleaned. Existing linked installs
are replaced automatically on the next bootstrap.

```bash
./scripts/bootstrap_native_linux.sh --prepare-only
flowork-browser-runtime --version
./scripts/native_dev_up.sh check-runtime
```

The native launcher checks that Browser CLI can start before bringing up the
services. A missing module or binary is an installation failure, even if the
extension is logged in. Startup errors now include a bounded, credential-redacted
runtime diagnostic rather than reporting every failure as a browser disconnect.

Docker builds already run `npm ci` from `api/playwright-runtime/package-lock.json`,
copy the runtime **with its node_modules**, and check its version. No separate
Chromium download is needed for controlling the user's Chrome through the
extension. Keep the native and Docker runtime version pins aligned.

The extension connects automatically. Its header shows the actual transport
state; a successful request to open a socket does not mean it is connected yet.
A 15-second heartbeat detects an unresponsive transport after 45 seconds and
reconnects with capped backoff. Transient reconnects and capability renewal
preserve the chat and draft. If a command was interrupted
after dispatch, inspect the page before retrying a write to avoid duplicate
submissions. For a public deployment, preserve WebSocket Upgrade headers on
`/api/v1/browser/ws` as described in [DEPLOY.md](../DEPLOY.md).


## 常驻部署的资源配额

常驻部署需要 cgroup v2 委派。CPU、内存配额、切流期间容量检查、原生 systemd 启动方式与 Docker 配置见 [Resident deployment resources](resident-deployments.md)。

### Prepare native frontend updates before restarting

Frontend changes must include both `web/package.json` and `web/pnpm-lock.yaml`.
The lockfile now includes `remark-math`, `rehype-katex` and KaTeX for message
and Markdown-file formulas; KaTeX CSS/fonts are bundled in the web build, with no external math CDN
or additional operating-system package required. Docker builds already install
this same lockfile in `web/Dockerfile`.

PDF previews also require the locked `pdfjs-dist` package's CMaps, standard
fonts, ICC profiles and decoder resources. `web/scripts/pdfjs-assets.ts` includes
them in every Vite build under `assets/pdfjs/<package-version>/` and serves them
in development. Publish the entire generated `dist` directory, including these
subdirectories; copying only JavaScript/CSS leaves some PDFs blank or missing
text. These resources use the deployment's own origin and base path, not a CDN.

For a native systemd installation, install dependencies and build as its service
account **before** stopping the running service:

```bash
sudo python3 scripts/prepare_native_web.py --repo /opt/flowork --user flowork
```

If an earlier root-run pnpm command left root-owned dependencies, repair their
ownership and prepare the build in one step:

```bash
sudo python3 scripts/prepare_native_web.py --repo /opt/flowork --user flowork --repair-ownership
```

This command runs frozen-lockfile installation, the production frontend build,
and the deployment-path check. It exits on failure and does not restart the
service. After it succeeds, drain active work and restart the existing service.
Use the actual deployment checkout, not a staging directory whose `node_modules`
is a symlink into a different checkout; pnpm records both its package store and
virtual-store locations. Do not install into a service-owned checkout as root.

## Persistent POSIX workspaces

The current POSIX backend covers Project/Chat files and private runtime state,
Workflow `/run` and chats, Task `/run` and result files, Deployment `/run`, and
user mounts. It does not replace PostgreSQL or the encrypted object store:
resource metadata, authorization, logs and traces still use the database, and
other blobs still require the object-store/KMS configuration. Sharing remains
host-authorized; never expose the workspace root directly to end users.

Select `WORKSPACE_STORAGE_BACKEND=posix` and one absolute
`WORKSPACE_STORAGE_ROOT` consistently in API, background worker and sandboxd.
POSIX is a filesystem interface, not an NFS server: use an encrypted local
volume on one host or mount the same shared filesystem on every runtime host.
The existing default `object_store` is a separate supported backend; changing
the environment variable does not migrate existing files.

### Fresh native installation

The bootstrap installs `gocryptfs`, `fuse3` and `nfs-common`. Prepare dependencies
first, then prepare the encrypted volume before starting services:

```bash
./scripts/bootstrap_native_linux.sh --prepare-only
sudo .venv/bin/python scripts/security/install_encrypted_workspace_volume.py \
  --root /var/lib/flowork/workspaces --user "$(id -un)"
```

Set these values in `.env.launch.local`:

```bash
WORKSPACE_STORAGE_BACKEND=posix
WORKSPACE_STORAGE_ROOT=/var/lib/flowork/workspaces
```

Then install/start the service as described above. The volume installer adds a
`flowork.service` dependency and stops that service if its encrypted volume
stops. Back up both the encrypted directory and its private mount key; losing
the key loses access to the data. Never commit the key.

For an existing encrypted NFS/block mount, use its normal mount service instead
of the gocryptfs helper. Give the application service account access, keep the
namespace root inaccessible to other users, and install the application unit
with `--workspace-root /absolute/mount/path` so systemd orders startup after
that filesystem. Configure mount failure handling in the mount service.

### Docker Compose

Prepare an encrypted host filesystem and give container UID/GID `10001:10001`
write access to the workspace directory (sandboxd also requires access).
Use ownership `10001:10001` and mode `2770` on the dedicated root so newly
created directories inherit the shared service group. Do not grant world access. Set
`WORKSPACE_STORAGE_HOST_ROOT` to its absolute path in `.env`, then add the
POSIX override to every Compose invocation:

```bash
docker compose -f docker-compose.yml -f docker-compose.posix.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.posix.yml up -d --build
```

The override bind-mounts the same directory into API, worker and sandboxd,
without creating a missing host directory. Ensure the host mount is active
before starting containers and stop containers before unmounting it. A bind
mount alone does not encrypt data. Keep object-store, database and credential
volumes and their backup configuration in place.

### Existing data

Stop/drain writers before migration. With the application's database,
object-store and KMS environment loaded, run the existing
`inventory_posix_workspaces.py`, `plan_posix_workspace_migration.py` and
`apply_posix_workspace_migration.py` tools under `scripts/security/` (see each
`--help`). Review conflicts before applying; the apply step requires
`--writers-stopped` and validates copied file hashes. Keep the original store
until verification and backups are complete, then switch all services together.
Do not delete old files merely because the environment variable was changed.

### Dependency verification

Python 3.11.17, uv 0.12.23, Node.js 24.21.0, pnpm 10.34.4 and Codex CLI
0.157.1 are the current installation pins. The native bootstrap and container
builds install the runtime used by Goal and app-server conversations. After
updating dependency manifests, regenerate all three hashed runtime locks
(root, Engine and sandbox), then run:

```bash
python3 scripts/verify_dependency_locks.py
./scripts/sync_python_env.sh
./scripts/native_dev_up.sh check-runtime
```

Run the last two commands only on a prepared installation, as its service user.
`check-runtime` checks executable/browser availability; it is not a replacement
for login, sandbox startup, file preview and Workflow execution acceptance.

The native installer and Node container build stages pin npm 11.19.0 and apply
checksum-verified upstream fixes for its bundled `brace-expansion`, `undici`,
`ip-address`, and `tar` packages. Application dependencies continue to use their committed lockfiles.


### Local draw.io preview assets

Preview and export use the Web renderer inside the pinned draw.io Desktop
installation, served by the API from `/opt/drawio/resources/app.asar`. Native
bootstrap and the API container already install this package. For a custom
installation location, set `DRAWIO_ASAR_PATH` to its installation-owned ASAR
file. Missing assets return `drawio_renderer_unavailable`; the browser does not
fall back to a third-party renderer. Opening the external editor remains an
explicit, separate user action.

For event-driven Deployment result waiting behind transaction-mode PgBouncer,
set `STATE_NOTIFICATION_DATABASE_URL` to a direct or session-pooled URL for
the same application database. PostgreSQL LISTEN requires session affinity.
