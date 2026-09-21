# Installation

Flowork supports two local installation methods. Docker Compose is recommended
for evaluation and self-hosted use. The native Linux setup is intended for
contributors who need to work directly with the source code.

| Method | Recommended for | Host requirements |
| --- | --- | --- |
| **Docker Compose** | Evaluation and local self-hosting | Docker Engine or Docker Desktop with Compose v2 |
| **Native Linux / WSL** | Development and debugging | Debian, Ubuntu, or WSL with `apt`, `sudo`, and network access |

Production deployments have different security and release requirements. Do
not expose the local stack directly to the Internet; use the
[production deployment guide](../DEPLOY.md) instead.

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
| CPU available to Docker | 4 vCPU | 8 vCPU |
| Memory available to Docker | 8 GiB | 16 GiB |
| Free disk before installation | 20 GiB | 40 GiB or more |

Docker Desktop users must allocate these CPU and memory resources to the Linux
engine; host totals alone are not sufficient. Disk capacity must cover the
repository, images and build cache, database and object-store growth, and
sandbox snapshots. A GPU is not required.

The preflight checks the Docker allocation and available repository filesystem
space before a build starts. A deliberately undersized test installation can
set `FLOWORK_ALLOW_UNSUPPORTED_RESOURCES=true` for that invocation, but such a
deployment is outside the supported configuration and may fail when an Agent,
LibreOffice, or diagram renderer runs. The host must also support privileged
Linux containers and the gVisor startup probe; compatibility is verified
automatically before the API begins accepting traffic.

Docker installation does not require Python, Node.js, or pnpm on the host. All
application dependencies are built into the project images.

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
7. verifies the Web, API, `sandboxd`, and gVisor checkpoint/restore path.

The first build can take several minutes. When verification succeeds, open
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

If `.env` already exists, the initializer preserves its contents. Do not delete
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
| Validate the resolved Compose file | `./scripts/deploy/local_server.sh config` |
| Run prerequisites only | `./scripts/deploy/local_server.sh preflight` |

Stopping the stack does not remove its named volumes. The database, object
store, runtime files, and generated `.env` remain available for the next start.

### Docker Desktop and WSL2

When Docker Desktop provides the daemon through WSL2 integration, use that
daemon directly. Do not install a second Docker daemon inside the WSL
distribution: the two daemons use different sockets, images, and volumes.

It is normal for `systemctl is-active docker` to report an inactive service in
this configuration. `docker info` must still succeed from the shell where
Flowork is started. The startup probe determines whether the Docker Desktop
kernel supports the required gVisor mode.

## Native Linux or WSL

The native installer supports Debian, Ubuntu, and WSL distributions that use
`apt`. Run it as a normal login user. The script invokes `sudo` only when it
installs operating-system packages; do not run the entire script as root.

### Install and start

Clone the repository, then run the bootstrap:

```bash
git clone https://github.com/LYuhang/flowork.git
cd flowork
./scripts/bootstrap_native_linux.sh
```

The bootstrap installs the required system packages, Node.js, pnpm, Codex CLI,
uv, Python dependencies, frontend dependencies, the pinned gVisor runtime,
headless LibreOffice, Poppler, and the checksum-verified draw.io Desktop CLI.
LibreOffice renders Word and PowerPoint files and provides headless office-file
conversion and validation for document-generation workflows; native XLSX files
are displayed by the browser workbook renderer. Poppler renders PDF pages. The
draw.io CLI and its disposable Xvfb display produce the image feedback used to
review native diagrams. The
installer verifies these runtime commands before creating the local
configuration and starting Flowork.

To prepare the environment without starting services:

```bash
./scripts/bootstrap_native_linux.sh --prepare-only
```

Start the prepared installation later with:

```bash
./launch.sh start
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
  poppler-utils
```

These are non-GUI packages; a desktop LibreOffice installation is not
required. Native Diagram feedback additionally requires the pinned draw.io
Desktop package and `flowork-drawio-export` wrapper installed by
[`bootstrap_native_linux.sh`](../scripts/bootstrap_native_linux.sh). For a
manual installation, follow that script's architecture selection, SHA-256
verification, package-metadata checks, and wrapper installation rather than
downloading an unverified latest package.

Verify that the commands used by the document and diagram runtimes are
available:

```bash
libreoffice --version  # `soffice --version` is also accepted
pdftoppm -v
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

Set `VIBECANVAS_NATIVE_RUNTIME_DIR` before running `launch.sh` if process state
and logs should be stored somewhere other than `/tmp/vibecanvas-native`.

### Manage the native stack

| Action | Command |
| --- | --- |
| Start | `./launch.sh start` |
| Stop | `./launch.sh stop` |
| Restart | `./launch.sh restart` |
| Show status | `./launch.sh status` |
| Show recent logs | `./launch.sh logs` |

For manual dependency installation, test commands, and frontend development,
continue with the [development guide](development.md).

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
optional. Settings controls the default Runtime for new Chats and the available
account/API sources. A new Chat selects its concrete source, model, and
model-supported thinking level in the composer. The first accepted turn fixes
the exact account/API connection for that Chat; subsequent turns may change
the model and thinking level only within that connection.
OpenRouter uses `VIBECANVAS_PUBLIC_URL` as its fixed OAuth callback origin, so
that value must match the address users open in the browser. A connected
OpenRouter account supplies its compatible text and tool-calling models to both
enabled Runtimes. Codex uses the Responses API through Flowork's host-side
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
| `VIBECANVAS_HTTP_PORT` | `9001` | Web application port |
| `OBJECT_STORE_PROVIDER` | `filesystem` | Local file-backed object storage; production deployments normally use `s3` |
| `SANDBOX_EGRESS_MODE` | `proxy` | Routes sandbox HTTP(S) and WebSocket traffic through the controlled egress proxy |
| `SANDBOX_EGRESS_POLICY` | `public` | Controls whether sandboxes may reach public destinations, an allowlist, or platform services only |
| `FLOWORK_CONTROL_PLANE_HTTP_PROXY` | (empty string) | Optional HTTP(S) proxy for host-side provider OAuth, catalogs, and metadata; unrelated to sandbox egress |
| `MOUNT_PATH` | (empty string) | Optional trusted host directory exposed through each user's isolated `/mount` path |

Keep `VIBECANVAS_INTERNAL_BIND_ADDRESS` at its `127.0.0.1` default. It protects
API diagnostics, databases, authorization services, queues, and metrics from
being published with the Web entry point. The full advanced template is
documented inline in [`.env.example`](../.env.example). Restart the affected
services after changing configuration.

Keep generated passwords, signing keys, browser token secrets, and encryption
keys out of Git, shell history, logs, and issue reports. Back up secret material
separately from persistent data. Production secret management, storage, and
egress requirements are covered in [Production deployment](../DEPLOY.md).

For the design behind the sandbox modes and network controls, see
[Sandbox lifecycle](architecture.md#sandbox-lifecycle) and
[Network boundaries](architecture.md#network-boundaries).

## Updating a source installation

Flowork is currently in alpha. Review release notes and back up PostgreSQL,
object storage, and the environment secret file before updating.

For Docker Compose:

```bash
git pull --ff-only
./scripts/deploy/local_server.sh up
```

The launcher rebuilds changed images, applies migrations, and verifies the
resulting stack.

For a native installation:

```bash
git pull --ff-only
./scripts/bootstrap_native_linux.sh --prepare-only
./launch.sh restart
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
the gVisor checkpoint/restore path:

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
| A service port is already in use | Change the corresponding `VIBECANVAS_*_PORT` value in `.env`. When changing the Web port, update `VIBECANVAS_PUBLIC_URL` as well. |
| `sandbox_prewarm` or checkpoint/restore fails | Inspect the `sandboxd` logs. Confirm that the active Docker kernel permits privileged containers and the configured gVisor platform. |
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
