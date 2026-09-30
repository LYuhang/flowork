# Architecture

Flowork is a self-hosted platform for turning agent-assisted work into
inspectable, reusable automation. Its architecture separates two concerns: the
control plane handles authentication, authorization, orchestration, and
persistent data; the execution plane runs agent and workflow code in isolated
environments.

This document describes the current architecture as implemented in the
repository. Installation and configuration belong in the
[installation guide](installation.md); production requirements belong in the
[deployment guide](../DEPLOY.md).

## Architecture overview

The architecture follows four principles:

1. **A shared workflow representation.** Agents and users edit the same
   versioned graph shown on the canvas. There is no separate agent-only workflow
   format.
2. **Separation of orchestration and execution.** The API coordinates work and
   records its state, while `sandboxd` runs agent and workflow code outside the
   API process.
3. **Clear storage responsibilities.** PostgreSQL stores application records,
   object storage holds file content, DBOS persists durable background work in
   PostgreSQL, and Valkey provides transient fanout and coordination.
4. **Server-side authorization.** The backend checks access before database,
   storage, model, or browser operations. A resource identifier supplied by a
   browser or sandbox is never treated as proof of access.

```text
Browser UI ── HTTP / SSE ──┐
                           ▼
Chrome extension ── WS ── Web / nginx ──► FastAPI control plane
                                               │
                 ┌─────────────────────────────┼──────────────────────┐
                 ▼                             ▼                      ▼
       PostgreSQL / OpenFGA /        DBOS background worker         sandboxd
      DBOS / object storage          + transient Valkey                │
                                                                       ▼
                                                         configured sandbox
                                                         Agent / Workflow code
```

The React application provides Chat, canvas, Task, Deployment, Storage, and
administration pages. FastAPI authenticates requests, checks access, stores
application data, coordinates background jobs and sandboxes, and streams
updates to clients. The default Compose deployment and its service connections
are defined in [`docker-compose.yml`](../docker-compose.yml).

## Core domain model

| Concept | Role | Current implementation |
| --- | --- | --- |
| **Organization** | Ownership and RLS boundary used to isolate application resources; an account may work in a personal or business organization | [Organization models](../api/src/vibecanvas_api/storage/models_org.py) |
| **Resource access** | Object-level ownership and grants for Workflows, Tasks, Deployments, and Knowledge packages; direct sharing does not move the resource | [Resource access API](../api/src/vibecanvas_api/routes/resource_access.py) |
| **Project** | User-owned workspace sharing files, one sandbox, and one Codex app-server across its Chats | [Project and Chat models](../api/src/vibecanvas_api/storage/models.py) |
| **Chat** | Independent conversation and Codex thread within a Project, with its own messages, commands, generation settings and persistent `chats/<chat-id>` working directory. Browser-extension conversations each get a dedicated Project using the same ownership model. | [Chat models](../api/src/vibecanvas_api/storage/models.py) |
| **Agent Run** | Persisted record of one agent response, including ordered events, approval waits, cancellation, and final status | [Agent Run models](../api/src/vibecanvas_api/storage/models_agent_runs.py) |
| **Workflow** | Reusable automation graph composed of validated nodes and references | [Workflow model](../api/src/vibecanvas_api/storage/models.py) |
| **Workflow Version** | Stored major/subversion snapshot used for history and version selection | [Workflow repository](../api/src/vibecanvas_api/storage/workflow_repo.py) |
| **Workflow Run** | One execution of a workflow or node, with status and events that the UI can reload | [Execution API](../api/src/vibecanvas_api/routes/executions.py) |
| **Task** | Persistent record for batch or scheduled work, including progress, results, and cancellation | [Task models](../api/src/vibecanvas_api/storage/models_tasks.py) |
| **Deployment** | API or webhook interface bound to the workflow head or a specific version | [Deployment model](../api/src/vibecanvas_api/storage/models_deployments.py) |
| **VFS** | Virtual file system that presents logical paths while storing file content in the configured object store | [VFS store](../api/src/vibecanvas_api/storage/vfs_store.py) |

## Components and responsibilities

### Web application

The React and Vite application provides the user interface and manages
browser-local interaction. Its main areas include Agent Chat, the Workflow
canvas, Tasks, Deployments, Knowledge, MCP servers, Skills, Storage, and
Settings. Routing and page composition start in the
[application router](../web/src/app/router.tsx), while the visual editor is
implemented under the [canvas pages](../web/src/pages/canvas/).

The Web application communicates with the API over HTTP and server-sent events.
Authorization is enforced by the API, and workflow code is never executed in
the browser.

### FastAPI control plane

The FastAPI service is the central entry point for application operations. It
is responsible for:

- authentication, sessions, and active organization selection;
- OpenFGA permission checks and PostgreSQL tenant isolation;
- Chat, Workflow, Task, Deployment, Knowledge, and VFS APIs;
- Agent Run coordination, approval state, and client event streams;
- secure handling of credentials and temporary access tokens;
- communication with DBOS workers and `sandboxd`.

The application factory shows the complete router and service composition in
[`app.py`](../api/src/vibecanvas_api/app.py). HTTP and streaming contracts are
grouped under [`routes/`](../api/src/vibecanvas_api/routes/).

### Agent Runtime

A Project inherits the default Runtime from **Settings → Agent runtime** when
it is created. Codex is currently
the built-in Runtime. The Project owns the sandbox, resident app-server and
exact account or API connection; each Chat owns a separate native thread.
Users may switch a Chat's model and reasoning effort within the Project's
connection between idle turns. The API sends runtimes the same internal request format,
and each adapter translates that
request into the format expected by its SDK. SDK-specific state and event
formats are therefore not exposed to the rest of the application. The common
request contains the message, attachments, selected model, active commands,
Model Context Protocol (MCP) connections, Skills, todo state, and references to
interactive artifacts.

Model discovery follows one explicit compatibility chain: API source,
provider, Runtime protocol, concrete model, and model-supported reasoning
levels. The [compatibility registry](../api/src/vibecanvas_api/services/agent_runtime/compatibility.py)
owns the source/Runtime mapping, while the [capability catalog](../api/src/vibecanvas_api/services/agent_runtime/capabilities.py)
projects only compatible models to the Chat composer. Provider credentials stay
on the Host and calls from the sandbox pass through the
[Runtime Model Broker](../api/src/vibecanvas_api/routes/runtime_model_broker.py).
For OpenRouter, the broker keeps Codex on the Responses API while translating
newer Codex namespace and hosted-tool descriptors into OpenRouter's documented
OpenResponses vocabulary. It restores function-call identities on the return
path, so the Runtime and sandbox see the same tool contract regardless of the
provider transport. Hosted Web Search remains capability-driven: the broker
enables it only when OpenRouter reports `web_search_options` for the selected
model, while ordinary sandbox and MCP tools remain available independently.
The first accepted turn fixes the Project row's exact non-secret connection
identity. This prevents provider-native history and credentials from crossing
accounts, while allowing later turns to select another model or reasoning level
within the same connection. New Chats inherit the Project connection and its
latest accepted model. Each Chat stores its own latest accepted selection
for Resume; each Agent Run stores an immutable snapshot of the Runtime,
connection, provider model, source, protocol, and reasoning effort used by that
turn.

The main application's **New Chat** action opens a local Project-scoped composer,
not a history entry. Repeated clicks reuse that Project's unsent draft, including
its text and attachments. The first accepted message promotes it into the
Project's conversation history. Uploading an attachment may allocate a durable
Chat directory before sending, but a Chat without messages remains absent from
history and the Project's conversation count. Draft identifiers and navigation
hints are account-scoped; no sandbox is started just by opening an empty page.

When enabled, **Agent Debug** reads the Chat's persisted conversation directly
from PostgreSQL, independently of the Project sandbox. Cursor pagination keeps
all user, assistant, and tool messages; long content is folded only in the UI.
The view also includes complete interactive artifact definitions, pinned preview
versions, current interaction state/results, and non-secret turn configuration,
status, timing, and recorded usage. It refreshes asynchronously after a user
message is accepted and after the Agent finishes, rather than querying on each
token. Debug does not generate snapshot files; users can explicitly download a
ChatML-style JSON export. This is the platform's stored transcript, not a dump
of the model's private internal context.

This common interface is defined in the
[runtime protocol](../api/src/vibecanvas_api/services/agent_runtime/protocol.py).
Runtime selection and conversion into the application's common event format
are centralized in the
[runtime orchestrator](../api/src/vibecanvas_api/services/agent_runtime/orchestrator.py);
the Runtime registry is defined in
[`registry.py`](../api/src/vibecanvas_api/services/agent_runtime/registry.py),
and the Codex adapter lives in
[`codex_runtime.py`](../api/src/vibecanvas_api/services/agent_runtime/codex_runtime.py).

### Workflow engine

`vibecanvas-engine` validates and executes workflow graphs independently of
FastAPI, the Web application, and the application database. It checks graph
structure, node relationships, and input references. Each node class defines
the validation and execution behavior for one supported node type.

The graph implementation is in
[`workflow.py`](../engine/src/vibecanvas_engine/workflow.py), and the node
catalog is under [`nodes/`](../engine/src/vibecanvas_engine/nodes/). The API
provides the surrounding permission checks, persistence, event streaming, and
sandbox integration.

The selectable Chat Runtime does not use LangChain. The workflow
`SubAgentNode` uses a small, lazily loaded LangChain/LangGraph loop directly
inside the workflow sandbox. Its native tools are `bash` (files and scripts),
`web_search` (source discovery), and `read_images` (image pixels), plus an
internal `set_output` tool generated from the node's declared output fields.
There is no second sandbox, Chat environment-flag requirement, MCP adapter,
persistent checkpointer, or tool-definition conversion layer. Shell calls use
the run's working directory, return exit status and both output streams, and
retain large output in files. Tool images are projected into multimodal model
messages without a shared pending-image queue. Valid result submission ends
the loop; invalid fields return feedback for correction.

### Workers

The DBOS worker performs batch execution, scheduled Task runs, Deployment
invocations, derived Knowledge indexing, and maintenance work. DBOS persists
queue and recovery state in PostgreSQL. Valkey remains only for short-lived
event fanout, rate limits, counters, and locks. PostgreSQL also stores the Task
and event history shown after a page refresh or service restart.

DBOS owns durable delivery and scheduling, not exactly-once external effects.
Batch and scheduled executions additionally claim a business row under a lock,
with a unique worker token and heartbeat. Duplicate delivery cannot claim an
already-started attempt, and later writes must still match the current owner.
Recovery first revokes the old worker's write authority and confirms its
sandbox has stopped, then records an unknown outcome without automatically
replaying the attempt. This heartbeat is a liveness check, not an execution
duration limit. Integrations must still handle external side effects with
idempotency keys where needed. Cancellation, partial results, and eligibility
for explicit resume remain in Flowork's business tables.

DBOS workflow arguments contain opaque Flowork record identifiers only. Batch
inputs, Deployment API/Webhook payloads, scheduled inputs, and Knowledge file
metadata remain in the application database or encrypted object store and are
loaded by a worker adapter at execution time. DBOS therefore does not become a
second plaintext store for private content.

The runtime-neutral submission interface is
[`background_queue.py`](../api/src/vibecanvas_api/services/background_queue.py),
DBOS registrations are in
[`background_workflows.py`](../api/src/vibecanvas_api/background_workflows.py),
and business implementations live under
[`background_tasks/`](../api/src/vibecanvas_api/background_tasks/).
Task ownership is implemented in
[`task_worker.py`](../api/src/vibecanvas_api/services/task_worker.py), with
reconciliation in
[`task_recovery.py`](../api/src/vibecanvas_api/background_tasks/task_recovery.py).

### Sandbox service

`sandboxd` owns sandbox processes, active sessions, mounted directories,
agent runtimes, and controlled network access. The API and workers request
execution through its private service interface rather than launching sandboxes
themselves. `SANDBOX_RUNTIME` selects the backend; `SANDBOX_TYPE` selects its
session mode. These are separate settings.

Native, Compose, release and image-only deployments default to bubblewrap
with rootless warm sessions. In Compose, `sandboxd` is the only privileged
application service so it can create nested namespaces. The bubblewrap backend uses Linux
namespaces and shares the host kernel rather than providing gVisor's
userspace-kernel boundary. It supports resident workers and Agent/Workflow
execution, but not process checkpoint/restore or post-start dynamic mounts.
Its current read-only host `/proc` binding also does not provide PID hiding.
gVisor is an explicitly selected alternative with a different isolation boundary
and optional checkpoint/restore; release promotion does not change the backend.

The daemon interface is implemented in
[`service.py`](../api/src/vibecanvas_api/services/sandbox/service.py), session
lifecycle and resource management in
[`manager.py`](../api/src/vibecanvas_api/services/sandbox/manager.py), and the gVisor provider in
[`gvisor.py`](../api/src/vibecanvas_api/services/sandbox/gvisor.py).
The alternative backend is
[`bubblewrap.py`](../api/src/vibecanvas_api/services/sandbox/bubblewrap.py);
[`coordinator.py`](../api/src/vibecanvas_api/services/sandbox/coordinator.py)
reports the selected backend's capabilities.

### Browser extension

The optional Chrome MV3 extension embeds Chat in a browser side panel and opens
an authenticated WebSocket connection limited to the current browser-control
session. Each extension Chat has its own automatically created Project. The
Agent uses `flowork-cli browser` in that Project's sandbox; its runtime
uses pinned `playwright-core` to control the user's existing browser, not a
second browser launched in the sandbox. The extension service worker is its
scoped remote CDP data plane: a relay allow-list attaches approved tabs, forwards CDP
messages, and reports tab lifecycle events. It has no Flowork-specific DOM
query/action protocol and never evaluates Agent scripts in the extension worker.
Page scripts run in authorized webpages; Playwright scripts run in the sandbox.

The relay allow-list and dispatch live in
[`relay-executor.ts`](../extension/src/playwright/relay-executor.ts), the
upstream-derived target model in
[`browser-model.ts`](../extension/src/playwright/browser-model.ts), and Chrome
integration in
[`service-worker.ts`](../extension/src/service-worker.ts). Build-time origins
and runtime sender checks share the allowlist in
[`config.ts`](../extension/src/shared/config.ts).

## Main runtime flows

### Chat turn

```text
User message
    │
    ▼
Authorize Chat and resolve the leading Slash Command
    │
    ▼
Create an Agent Run and assemble a common runtime request
    │
    ▼
Reuse or restore the Project sandbox, then start or resume the Chat thread
    │
    ▼
Persist ordered events ──► stream updates to the client over SSE
    │
    ├── record and wait for approval or interaction
    └── complete, cancel, or fail the Agent Run
```

The Chat API authenticates the user, checks access to the Chat, resolves the
Slash Command available in the main application or extension, and loads the
Runtime bound to its Project. It then creates an Agent Run as the system of
record for the turn. The runtime orchestrator calls the appropriate adapter and
converts its output into a unified event schema.

Events are persisted before they are sent through Server-Sent Events (SSE).
The client can therefore reconnect, reload missed events, or resume an approval
flow without relying on the original HTTP connection.

Relevant code paths are the [Chat API](../api/src/vibecanvas_api/routes/chats.py),
[command registry](../api/src/vibecanvas_api/agents/commands/registry.py),
[Agent Run repository](../api/src/vibecanvas_api/storage/agent_runs_repo.py),
and [approval repository](../api/src/vibecanvas_api/storage/hitl_repo.py).

### Agent tools and MCP integration

Slash Commands provide task-specific operating guidance for a Chat.
Every resident Project workspace owns one aggregate MCP Hub. Its Chats share
the workspace and app-server. Extension conversations automatically create a
one-Chat Project; there is no separate per-Chat runtime ownership model.
Custom MCP selection and its revision are Project-owned. Repeated selection
of the same server is idempotent; each accepted Turn snapshots the selection,
while invocation authority and user approvals remain Chat/Turn-specific.
Codex connects to the Hub's
single loopback Streamable HTTP endpoint. Base capabilities are always
projected; commands such as `/workflow`
and `/task` teach authenticated Platform CLI workflows, while `/diagram` and
`/document` teach specialized sandbox CLI workflows. `/browser` is available
only in the extension side panel.

The Hub owns external MCP discovery, local MCP processes, remote MCP client
sessions, and tool naming. Built-in business operations use a separate sandbox
command channel. The Host derives user, tenant, Chat, and active Turn identity
from trusted state, then applies the same resource authorization used by the
Web API; command arguments do not establish identity. The sandbox does not
receive long-lived platform credentials. Remote MCP credentials also remain
on the Host, which applies credentials and egress controls to upstream requests.
Browser automation has migrated
from Playwright MCP to `flowork-cli browser`; its sandbox worker uses
`playwright-core` directly and is not an MCP server. Document and Diagram also
use sandbox CLI runtimes. The built-in MCP surface retains `render_preview`
for file, URL and pinned Workflow cards, and `render_choices` for a tool call
that waits for user selection. Retired business MCP tools are not advertised.

Document and Diagram commands also enforce a small deterministic completion
boundary. The Agent must validate the exact current file revision (and inspect
rendered feedback for visual office formats); once that evidence is current,
the sandbox Hub publishes the same revision to Preview. A later file mutation
invalidates the earlier evidence, so a stale review or Preview cannot approve a
newer file silently.

The boundary is implemented by the [secret-free Runtime contracts](../api/src/vibecanvas_api/services/agent_runtime/mcp_runtime_protocol.py),
[Host authority resolver](../api/src/vibecanvas_api/services/agent_runtime/mcp_host_resolution.py),
[sandbox Hub](../api/src/vibecanvas_api/services/agent_runtime/mcp_hub.py),
[Hub adapters](../api/src/vibecanvas_api/services/agent_runtime/mcp_hub_adapter.py),
and [Host Gateway](../api/src/vibecanvas_api/services/agent_runtime/mcp_host_gateway.py).
The command authorization adapter is
[`resource_routes.py`](../api/src/vibecanvas_api/services/agent_runtime/resource_routes.py).
Built-in render tool schemas and invocation logic live in
[`platform_mcp/invocation.py`](../api/src/vibecanvas_api/services/platform_mcp/invocation.py).

### Workflow editing and execution

```text
Canvas or /workflow
    │
    ▼
Validate graph and node references
    │
    ▼
Save workflow and version state
    │
    ▼
Submit a workflow or node execution
    │
    ▼
Run through sandboxd and vibecanvas-engine
    │
    ▼
Persist status and events; write large data to the run-specific VFS
```

Chat tools and the canvas read and update the same persisted Workflow model.
Before execution, the API validates the graph and applies an admission policy:
only node types supported by the workflow engine are sent to the sandbox.
Operations that require backend access remain in control-plane services.

For interactive runs, PostgreSQL stores only the status and ordered events
needed to restore the canvas after a refresh. Large inputs, outputs, and files
are stored in a VFS namespace dedicated to that run. This keeps control-plane
records small while preserving execution data for later inspection.

This flow is implemented in the [execution routes](../api/src/vibecanvas_api/routes/executions.py),
[sandbox admission policy](../api/src/vibecanvas_api/services/sandbox/workflow_guard.py),
[Workflow Run models](../api/src/vibecanvas_api/storage/models.py), and
[run-scoped VFS repository](../api/src/vibecanvas_api/storage/vfs_run_repo.py).

### Tasks and deployments

A Task is the persistent job record for batch execution (`batch_exec`) or a
scheduled run (`scheduled_run`). Its event log contains status changes,
progress, logs, results, and the final outcome. A Task schedule adds cron or
interval timing, input presets, concurrency policy, and notification settings.

A Deployment exposes a Workflow as one of two external trigger types:

- **API**, authenticated with a deployment API key;
- **Webhook**, authenticated with an HMAC secret.

Recurring and calendar-based execution is modeled as a scheduled Task rather
than a Deployment. This keeps external serving concerns separate from workload
scheduling and gives scheduled work the Task lifecycle, history, and controls.

A Deployment can follow a selected major version's latest saved subversion or
pin a full version; older deployments may retain a global-head policy. Each
invocation freezes its resolved graph before execution. Synchronous API requests
wait for sandbox execution and return its result; asynchronous API and Webhook
requests submit durable work to DBOS and return an invocation identifier.
The authenticated application test action also runs the deployment without
requiring an Agent to possess its external API key. Workflow code always runs
through the sandbox service, not directly in the API or worker process.

The `flowork-cli task` and `flowork-cli deployment` commands expose observability data
through file-oriented diagnostic exports. A Task export contains the current
resource state, exact event counts, searchable JSONL events, and—when
applicable—scheduled execution history. A Deployment export contains its
current configuration, bucketed call/error/latency metrics, and cursor-paginated
invocation logs. The Agent can inspect these ordinary sandbox files with its
normal search and scripting tools without placing a large log stream in model
context. Export calls remain read-only and use the existing `INSPECT_RUNS`
authorization boundary.

See the [Task API](../api/src/vibecanvas_api/routes/tasks.py),
[Deployment API](../api/src/vibecanvas_api/routes/deployments.py),
[invocation routes](../api/src/vibecanvas_api/routes/deployment_invoke.py), and
[deployment worker](../api/src/vibecanvas_api/background_tasks/deployment_invoke.py).

### Browser control

`/browser` is available only in a Chat opened from the extension side panel.
The extension establishes an authenticated control channel, and the backend
stores which Chat currently holds that browser session. The selected Agent
Runtime starts the Browser CLI worker inside the Project sandbox.
Playwright owns page snapshots, locators, actionability, waiting, dialogs, tabs,
and screenshots; Flowork owns the CLI schema and authorization. Its CDP connection is carried through a
short-lived, Chat- and generation-fenced WebSocket capability to the extension;
the browser never exposes a public debugging port.

The sandbox starts a worker on demand for the active Turn and connects it to a local
[CDP relay](../api/src/vibecanvas_api/services/agent_runtime/mcp_browser_transport.py).
For each active Turn, the Host Gateway supplies a short-lived upstream binding
that passes through the authenticated [browser relay route](../api/src/vibecanvas_api/routes/browser.py)
and reaches the selected extension through the
[transport registry](../api/src/vibecanvas_api/browser/registry.py). Explicit
`--tab_id` targets replace a mutable current-tab binding. Command schemas live in
[`browser_cli.py`](../api/src/vibecanvas_api/flowork_cli/browser_cli.py), and
[`browser_cli_runtime.py`](../api/src/vibecanvas_api/services/agent_runtime/browser_cli_runtime.py)
reauthorizes each command and ends the worker when the Turn ends or authority
is revoked. `eval` runs in the authorized page; `run-code` runs Playwright code
in the sandbox. Neither bypasses tab ownership. Cookie export needs separate
site-specific consent. File-producing commands return persistence receipts;
observation, cancellation or storage failures must not silently repeat page actions.

## Data and state management

| Component | Primary role | Notes |
| --- | --- | --- |
| **PostgreSQL** | System of record for Organizations, users, Projects, Chats, messages, Workflows, versions, runs, approvals, Tasks, Deployments, metadata, and ordered events | Tenant-specific business tables use row-level security |
| **OpenFGA** | Relationship-based access control (ReBAC) | Evaluates whether a user can perform an action on a resource |
| **Object storage** | File content for VFS, artifacts, authoritative Knowledge package files, Task outputs, and run files | Filesystem and S3 backends implement the same storage interface |
| **DBOS / PostgreSQL** | Durable background queue, recovery, and schedules | Uses the existing application PostgreSQL server; business state remains in Flowork tables |
| **Valkey** | Transient coordination | Carries short-lived notifications, rate limits, counters, and locks; it is not the task broker or system of record |
| **Runtime state** | Separate native Chat threads inside a Project-owned authenticated Runtime volume | Persists independently of live network connections without exposing SDK internals to the API |
| **Runtime volumes and snapshots** | Project runtime files and optional gVisor checkpoints | Used to resume execution efficiently, not to determine identity or permissions |

VFS metadata and file content are separated by the
[VFS store](../api/src/vibecanvas_api/storage/vfs_store.py) and
[object-store providers](../api/src/vibecanvas_api/services/object_store.py).
The Sandbox file explorer still reads this durable VFS view. While an
interactive Project sandbox is loaded, listing or manually refreshing its files
first reconciles the live workspace into VFS; recognized file mutations also
trigger an earlier best-effort writeback. Turn completion remains the final
durability boundary, so visibility does not depend on a particular Agent tool
name. See the [VFS route](../api/src/vibecanvas_api/routes/vfs.py), [Web query
polling](../web/src/lib/api/queries/vfs.ts), and [sandbox manager](../api/src/vibecanvas_api/services/sandbox/manager.py).
Runtime files use the same authenticated volume lifecycle in
[`vfs_volume.py`](../api/src/vibecanvas_api/services/vfs_volume.py);
resuming a Chat does not require a separate LangGraph checkpoint store.
Encryption and retention behavior are described in
[Security and data lifecycle](security-and-data-lifecycle.md).

## Authorization and execution boundaries

Flowork uses defense in depth for data access. OpenFGA provides
relationship-based access control (ReBAC): it determines whether the current
user may perform an action on a specific resource. PostgreSQL row-level
security (RLS) independently restricts database rows to the active tenant. Both
the authorization context and tenant context are derived from the authenticated
request; a sandbox cannot select its own tenant.

Accounts are global identities, while each resource retains the personal or
business organization that owns it. A direct grant to another account does not
change that ownership. For a shared resource, the server first resolves a
recipient-safe projection, admits exactly that resource in its owner tenant,
binds RLS to the owner tenant, and performs the normal OpenFGA check again.
Personal sharing resolves only an exact account email; business organizations
can additionally target entries in their own member and group directory. This
object-level sharing is available for Workflows, Tasks, Deployments, and
Knowledge packages, but not for installed Skills or MCP servers, catalog
entries, API credentials, or platform-built-in resources. See the
[shared-resource admission](../api/src/vibecanvas_api/auth/deps.py),
[resource access API](../api/src/vibecanvas_api/routes/resource_access.py), and
[provenance presentation](../api/src/vibecanvas_api/services/resource_provenance.py).

Host gateways use short-lived authority limited to the current organization,
user, Chat and Agent Run, with additional server scope for custom MCP
connections. Before performing a protected operation, the backend
checks the current database records and resource permissions again. Upstream
MCP credentials remain on the Host; the sandbox receives only a logical broker
route and a Turn-scoped execution capability.

The main implementations are the
[authorization service](../api/src/vibecanvas_api/authorization/openfga.py),
[tenant-bound database sessions](../api/src/vibecanvas_api/storage/db.py), and
[Agent resource authorization](../api/src/vibecanvas_api/services/agent_resources/authorization.py)
and [capability checks](../api/src/vibecanvas_api/services/agent_resources/capability.py).

## Sandbox lifecycle

When a snapshot-capable gVisor profile is enabled, `sandboxd` manages interactive
Project and Workflow Debug sessions through the following lifecycle:

```text
Released ── acquire ──► Warm ── idle ──► Hibernating ──► Hibernated
                          ▲                                  │
                          └──────────── Restoring ◄──────────┘

Warm / Hibernated ── release ──► Releasing ──► Closed
Hibernating / Restoring ── failure ──► Snapshot failed ──► Releasing
```

`warm`, `hibernating`, `hibernated`, `restoring`, `releasing`,
`snapshot_failed`, and `closed` are lifecycle states. `busy` and `idle` are
separate activity observations while a session is warm. `released` is the
status reported when no session is currently loaded, not an internal session
state.

Agent idleness is measured for the entire Project, not the currently visible
Chat. Running and queued sibling turns, tool work, and pending writeback keep
the shared sandbox busy. The idle interval starts after the last activity
finishes; reading lifecycle status does not reset it. Individual Chats do not
pin or unpin the Project using a shared resident flag. Closing the runtime
transport releases its activity lease, including cancellation and disconnect.

Before hibernation, `sandboxd` finishes pending file writes, synchronizes the
runtime volume, and stops the Agent Runtime process. Live network connections
and temporary credentials for the current turn are therefore excluded from the
checkpoint. When the session resumes, these connections and credentials are
created again. Rootless warm and bubblewrap sessions do not restore a process
checkpoint: idle sessions are released, then cold-started on demand. Durable
files and conversation state are restored separately; this is not process-memory
continuity. Release preserves the Project connection binding and each Chat's
model, native thread state and persistent working directory; it does not unlock
the Project for a different account/API source. Do not configure snapshot mode
for a backend that does not support it.

An explicit release request is a quiescent boundary: it does not return until
the old Runtime process has stopped and the volume release has completed. This
prevents a new turn from reacquiring the same Project scope while stale cleanup is
still able to remove its restored Runtime state. Idle eviction may run in the
background. A same-Project acquire waits for automatic eviction cleanup to
finish before restoring its volumes; unrelated Projects do not wait for that
teardown.

Reusable baseline snapshots and Project-specific hibernation snapshots are stored
separately and cannot be used interchangeably. The lifecycle states and valid
transitions are defined in
[`session_lifecycle.py`](../api/src/vibecanvas_api/services/sandbox/session_lifecycle.py);
transition behavior and idle sweeping are implemented in
[`manager.py`](../api/src/vibecanvas_api/services/sandbox/manager.py).

## Network boundaries

The default Compose stack uses network segmentation: browser-facing traffic,
application data, authorization, sandbox control, and outbound traffic use
separate Docker networks. Web/nginx is attached only to the edge network. The
API reaches PostgreSQL and Valkey through the data network, OpenFGA through the
authorization network, and `sandboxd` through the control network. OpenFGA has
a separate network for its own database.

The default sandbox configuration uses `SANDBOX_NETWORK=none` and
`SANDBOX_EGRESS_MODE=proxy`. A controlled egress proxy handles outbound HTTP(S)
and WebSocket traffic. It allows public destinations according to policy and
private destinations only when they are explicitly configured. Host-network
mode is a development option for trusted workloads that require protocols the
proxy does not support.

The deployable topology is the source of truth in
[`docker-compose.yml`](../docker-compose.yml). Egress validation and relay
behavior are implemented in
[`egress_policy.py`](../api/src/vibecanvas_api/services/sandbox/egress_policy.py)
and [`egress_broker.py`](../api/src/vibecanvas_api/services/sandbox/egress_broker.py).

## SubAgent Workflow resources

SubAgent nodes declare optional `skills` and `mcp_servers` arrays. Each reference
contains only an installation `id` and a display `name`; IDs determine execution.
The editor supports multiple selections. The constructing Agent can discover
resources through `flowork-cli skill list/get/files/read` and
`flowork-cli mcp list/get/tools` before writing a Workflow.

Every execution resolves the latest published Skill contents once. A batch
shares one dependency snapshot. Read-only files are published atomically under
`/skills/<installation-id>/<content-hash>/` without replacing the mount root.
An existing execution keeps its immutable content while subsequent invocations,
including invocations of a resident deployment, resolve new publications.
The node prompt describes selected Skills; the mount is a soft isolation view
of resources authorized for that execution identity. Selection is not a file
access boundary. Task and deployment identities require explicit delegation.

Before a deployment candidate becomes ready, its persisted service account and
resource grants are checked, new authorized dependencies are delegated, and
Skill packages are decrypted and published into the resident read-only mount.
A private preparation lease retains these files until the first invocation
replaces it with a freshly resolved execution snapshot. Periodic maintenance
checks preparation permissions as well; it does not create execution tokens or
regrant revoked dependencies. A preparation failure keeps the old instance
serving traffic.

Private host lease records retain versions used by active executions. Sandbox
maintenance checks execution state and current resource authorization, then
removes obsolete or revoked files from the mounted view. It also runs for
resident sandboxes and does not extend their activity lifetime. The normal
maintenance interval is `SANDBOX_ACTIVITY_POLL_INTERVAL_S` (default 5 seconds),
plus authorization and scheduling latency. An authorization infrastructure
failure is logged and retried; it does not count as proof that access was
revoked. This is not instantaneous revocation of bytes a process already read
or copied. Other authorized concurrent executions retain their files.

Selected MCP tools become LangChain tools with stable namespaced names. Each
call uses a scoped execution capability to reach the host broker, which checks
live execution, resource permissions and the expected tool contract before
resolving credentials. API keys and OAuth tokens are not Workflow fields or
SubAgent prompt content. The broker uses the existing MCP Python SDK and
sandbox MCP client for stdio, Streamable HTTP and SSE transport. A stdio server
requires its executable and dependencies in the runtime environment.

MCP calls currently use isolated, short-lived sessions, with a separate limit
of four concurrent probes/calls per sandbox manager. Connections are closed
and temporary files reclaimed when the call completes or is cancelled; a
node cannot close another node's connection. The broker monitors cancellation
and authorization during a call. It does not automatically retry business
tools after failures. Connection configuration and tool contracts are checked
against the execution snapshot: changed contracts require a new execution.
The implementation uses existing `mcp`, `langchain-core` and `jsonschema`
dependencies from `api/pyproject.toml`; it adds no separate MCP daemon.

## Repository map

| Path | Responsibility |
| --- | --- |
| [`api/src/vibecanvas_api/routes/`](../api/src/vibecanvas_api/routes/) | HTTP, WebSocket, and SSE contracts |
| [`api/src/vibecanvas_api/services/agent_runtime/`](../api/src/vibecanvas_api/services/agent_runtime/) | Common runtime interface, adapters, and orchestration |
| [`api/src/vibecanvas_api/services/platform_mcp/`](../api/src/vibecanvas_api/services/platform_mcp/) | Agent tools that require backend access |
| [`api/src/vibecanvas_api/services/sandbox/`](../api/src/vibecanvas_api/services/sandbox/) | Sandbox service, lifecycle, gVisor, and egress |
| [`api/src/vibecanvas_api/storage/`](../api/src/vibecanvas_api/storage/) | Database models and repositories |
| [`api/src/vibecanvas_api/authorization/`](../api/src/vibecanvas_api/authorization/) | OpenFGA model and enforcement |
| [`api/src/vibecanvas_api/background_tasks/`](../api/src/vibecanvas_api/background_tasks/) | Runtime-neutral background job implementations |
| [`engine/src/vibecanvas_engine/`](../engine/src/vibecanvas_engine/) | Workflow graph and node runtime |
| [`web/src/`](../web/src/) | React application and visual Workflow editor |
| [`extension/src/`](../extension/src/) | Chrome MV3 side panel and browser-control bridge |

The main dependency direction is deliberate: the Web application calls the
API; the API coordinates the engine and external services; the engine does not
depend on the API or Web application.

For operational details, continue with
[Installation and deployment](installation.md). For trust assumptions,
retention, and vulnerability reporting, see
[Security and data lifecycle](security-and-data-lifecycle.md) and
[`SECURITY.md`](../SECURITY.md).
