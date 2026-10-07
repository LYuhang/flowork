# Resident deployment resources

Each deployment revision has its own resident bubblewrap instance. Its resource
budget covers the instance's descendants, including concurrent workflow workers
and terminal processes. A limit is not a reservation of physical RAM.
The 256 MiB starting size leaves room for rolling overlap on small hosts;
increase it for workflows with larger in-memory datasets.

Deployment terminals start in `/run`, a Deployment-owned persistent directory.
All workers and overlapping revisions of the same Deployment mount the same
host projection. File creation, modification and deletion are immediately shared;
individual invocation start/end never clears the directory. Each Deployment has
its own resource workspace keyed by Deployment ID, not revision or invocation.
With `WORKSPACE_STORAGE_BACKEND=posix`, every revision mounts that same durable
filesystem directory directly; no invocation copies or completion uploads are
needed. Storage encryption and backups belong to the mounted filesystem. The
object-store backend instead hydrates and writes back its shared projection;
changes not yet synchronized can be lost on unexpected host loss. Neither backend
restores in-memory execution after a crash; lost invocations are not replayed.
Deployment instances use an execution workspace profile: they do not initialize,
hydrate, mount or write back the Chat-specific `/chats`, `/data`, `/logs` and
`/memory` roots. Each invocation has independent in-memory execution state,
inputs, results, events and cancellation. File paths remain shared: workflows
are responsible for file naming and concurrent writes. The host installs a frozen workflow revision over authenticated
local RPC; inputs, execution events and results travel over RPC as well. Request
and result JSON files are not the execution transport.
Use `/mount` for durable files shared at user scope when explicitly enabled.
Deployment `/run` requires no mount opt-in. Persistence belongs to the Deployment workspace, not to per-invocation
artifact copies.
Workspace previews require that Deployment's inspect-runs permission.

## Configuration changes and rollout

The Settings page shows a confirmation with the previous and proposed values
before every save. Cancelling leaves the draft available for editing.

| Change | Effect after confirmation |
| --- | --- |
| QPS | Updates admission limits without replacing the instance |
| Workflow version, its dependencies, or `/mount` | Prepares a replacement instance |
| CPU, memory, worker count or per-worker concurrency | Prepares a replacement with the new limits |
| Disable | Stops accepting new calls; previously accepted calls drain |
| Enable | Prepares the deployment before accepting calls |

For mixed changes, the confirmation distinguishes immediate admission changes
from changes that require an instance replacement. Saved settings describe the
desired configuration; the Overview instance cards show what is currently
serving. `/mount` changes use replacement, not hot attachment to a running
instance. Dependencies come from the selected workflow version.

During replacement, the old instance continues serving while the candidate
prepares its execution environment. After readiness succeeds, a database
transaction switches admission to the new revision. Previously accepted queued
and running calls, including calls waiting for human approval, stay on the old
revision. It is removed only after those calls
and its local workers have finished. A failed candidate leaves the old revision
serving; insufficient capacity leaves the replacement waiting. Superseded
configuration cannot promote over a newer saved change.

Overview displays preparing, active and draining revisions, their resolved
workflow versions, mount state, pending calls and measured resource usage.
Unavailable or stale measurements display as unknown rather than zero.

## Resource controls

The deployment API accepts `cpu_millis` (1000 = one CPU) and `memory_mb` (MiB).
Defaults are 500 millicores and 256 MiB. Both create and patch validate integer
limits; the settings page exposes CPU cores and memory under Advanced.

`worker_count` defaults to `max(1, floor(cpu_millis / 1000))` on creation;
`worker_concurrency` defaults to -1, meaning unlimited. Both are configurable in Settings and via
CLI `--worker-count` / `--worker-concurrency`. Limits are 1–256 workers and either -1 (unlimited) or 1–64
simultaneous invocations per worker. These are scheduling limits, not additional
CPU/memory reservations. CLI/API changes specify each field explicitly; the UI
follows CPU-based recommendations until the user manually edits worker count.

A worker is one resident engine process. Native asynchronous model, SubAgent and
HTTP calls share that process; Code/Bash retain cancellable lightweight children.
The pool routes each new invocation to the least occupied worker, rotates ties,
and starts invocations directly without queuing. Only an explicitly configured
positive concurrency limit rejects excess invocations with HTTP 429; QPS and
tenant limits are independent. Approval waits and persistence keep
slots reserved. Cancelling one invocation never kills its siblings' worker.

Changing resource limits creates a replacement revision. Admission counts the
old, candidate and draining instances together. If their configured limits do
not fit the deployment pool, the candidate waits and the old instance remains
active. Increasing an instance limit therefore requires headroom for both
instances until draining finishes.

The runtime enforces `cpu.max`, `memory.max`, `memory.swap.max=0`,
`memory.oom.group=1`, and `pids.max=512` using cgroup v2. Memory exhaustion may
terminate that instance's workers. The controller can recover the instance;
requests that were executing may fail. These are hard limits, not guarantees
that every workflow can complete within the selected budget.

Once sandboxd accepts an invocation, it records completion itself before
releasing its local drain lease. Losing the API caller therefore does not
prevent a surviving executor from recording success or failure. Completion is
monotonic: an API acknowledgement or queue retry cannot reopen or overwrite a
terminal invocation. The deployment invocation summary contains counts; the
linked encrypted execution history also retains inputs, the frozen graph,
node events and outputs, approval decisions and final results in PostgreSQL.
The runtime retains events until the host commits them and acknowledges their
sequence. Transient database errors while storing an event batch retry that
same batch for up to 10 seconds; an uncertain commit is safe to retry because
event sequences are deduplicated. No success or runtime acknowledgement is
reported before the commit. A persistent failure cancels the affected invocation
before releasing its capacity; it does not replay the workflow.
These records have no automatic TTL deletion. Temporary process files and
short-lived credentials can be removed without deleting execution history.
Queued invocations remain bound to their admitted revision until execution.
For explicit asynchronous and webhook submissions, DBOS delivers the admitted
invocation to sandboxd and exits once sandboxd commits its ownership claim.
Approval waits therefore consume the deployment's reserved execution capacity,
not a background queue worker. A lost dispatch acknowledgement is reconciled
against the same invocation; an existing claim never starts a second execution.
The delivery task does not mark the invocation successful or clean its files.

Execution ownership is claimed atomically in PostgreSQL. The executor renews a
60-second lease every 10 seconds; failure to renew cancels only that request's
execution. The controller marks expired executions as failed, and local worker
leases plus cgroup emptiness checks still prevent premature instance removal.
This also releases durable drain blockers after a daemon is killed without
running its finalizers. Expired or completed calls are not automatically
re-executed, because their external side effects may already have happened.

Synchronous API and dashboard test calls have five minutes to reach the
executor after admission. Unclaimed calls past that deadline fail with
`dispatch_expired`. Background calls that are still queued have no dispatch
expiry; their execution lease starts only when sandboxd claims them. Apply
migrations through 153 before running this version, and drain active calls during an
upgrade from an older executor that does not renew leases.

## Human approval and API results

`HumanApprovalNode` requires `instruction`, `timeout_seconds` (a positive
integer) and, for deployments, an explicit `approver_email`. Admission resolves
the email to an active member of the deployment's organization. Its only output
is `approved: boolean`: true for a human approval and false for a human rejection.
Both decisions continue the graph; use a ConditionNode to route downstream work.
An approval timeout produces no business output and terminates that execution,
including active parallel branches. Its approval record has status `timeout`
and `approved=null`; its execution has status `timed_out` and error code
`approval_timeout`, distinct from ordinary `execution_timeout`. Timeout skips
credential refresh/resume, rejects late decisions, and releases capacity only
after the invocation has stopped. Other invocations and the shared worker remain
available. Cancelled/interrupted approvals also produce no decision.

A Deployment call that reached approval retains its asynchronous ticket. Result
queries return HTTP 200 for a successful query and expose `timed_out` with
`approval_timeout` in the payload; they do not restart the invocation.

Every visit creates an independent approval, including loop iterations. Waiting
retains execution capacity. A deployment call that reaches an approval returns
HTTP 202 with `invocation_id`, its compatibility alias `task_id`, and a
`Location`/`status_url`. `result_url` remains an alias for existing clients.
Tickets also include `async_reason` and `poll_after_seconds`; the latter equals
the `Retry-After` header (3 seconds). Reasons are `human_approval` and `explicit_async`. A synchronous invocation never
changes contract merely because HTTP observation takes longer or dispatch returns
before completion. Calls that take another branch can still complete
synchronously. The notification hook is currently a placeholder; review takes
place in the execution detail page linked from the deployment's Activity log.
The page shows the frozen graph and node results without granting graph-edit
permission. Only the assigned reviewer can approve or reject.

For API-triggered deployments, submit the StartNode input object directly to
`POST /api/v1/deployments/{slug}/invoke` with the deployment's Bearer API key.
`POST /api/v1/deployments/{slug}/runs` explicitly requests asynchronous execution.

| Situation | HTTP response |
| --- | --- |
| Synchronous execution succeeds | 200 with final EndNode outputs |
| Execution reaches human approval | 202 with the same invocation's ticket |
| HTTP observation reaches 30 seconds | 202; execution continues |
| Explicit asynchronous submission is admitted | 202 |
| Synchronous workflow execution fails | 502 with a safe error code |
| Active execution budget expires during synchronous observation | 504 |
| Invalid input | 422 |
| QPS or invocation concurrency quota is exhausted | 429 with `Retry-After` |
| Redis cannot enforce a configured QPS limit | 503 `rate_limit_unavailable`, with `Retry-After: 1`; no invocation is admitted |
| Execution infrastructure is unavailable | 503 with `Retry-After` |
| Internal dispatch fails | 500 |

Query `GET /api/v1/deployments/{slug}/runs/{invocation_id}` using the current
deployment API key. Query HTTP 200 means the lookup succeeded; inspect `status`
for `queued`, `running`, `waiting_approval`, `succeeded`, `failed`, `timed_out` or
`cancelled`. External results contain final EndNode outputs and safe errors;
`outputs` contains the EndNode business fields directly, without an `__end__`
wrapper. Successful queries include `error: null`; failed executions include
`error: {"code": "..."}`. The `error_code` and `errors` fields remain aliases for
compatibility. Internal graph data, node outputs and tracebacks belong to the authorized
execution detail view. Key rotation applies to queries of existing results too.

Public admission errors use the same `error.code` envelope. Codes distinguish
`rate_limit_exceeded`, `concurrency_limit_exceeded`, `executor_unavailable`,
`deployment_not_ready`, `invalid_input`, `internal_error`, and
`idempotency_conflict`. Execution failures use `execution_failed`; active budget
expiry uses `execution_timeout`. Internal storage codes are translated at the
HTTP boundary. Errors never include dependency exception messages or raw input.

Batch Tasks create execution history only after an input obtains a worker and
starts execution. Inputs still waiting for a worker have no execution log or
detail link. Approval waiting retains that worker, while other free workers
can continue processing inputs.

For retryable submissions, supply `Idempotency-Key` (1–256 visible ASCII
characters). Reuse the same key and input for one logical call across `/invoke`
and `/runs`; it returns the original invocation. Different input with the same
key returns 409. An HTTP disconnect does not cancel an admitted execution.

The HTTP observation window, approval deadline and active execution budget are
separate. Human-only waiting pauses the active execution budget; concurrently
running non-human branches still consume it. Approval deadlines are enforced
by the server. Duplicate or late decisions return 409. Continuation rechecks
the original execution principal's authorization, not the reviewer's identity.

Pending execution state is not checkpointed for sandbox restart recovery. If
the owning sandbox process is lost, the execution fails with `execution_lost`,
its pending approvals close, and it is not replayed. Capacity is released only
after process termination is confirmed. Persisted history remains queryable.

Webhook submissions also return HTTP 202 with an invocation ticket and
`Location`/`result_url`, pointing to
`/api/v1/deployments/{slug}/webhook/runs/{invocation_id}`. Query that URL with
`X-Vibecanvas-Timestamp` (Unix seconds) and `X-Vibecanvas-Signature`. Use the
current webhook secret to compute `sha256=` followed by the hex HMAC-SHA256 of
the UTF-8 string `timestamp + ".GET " + result_url`. The signed path starts at
`/api/v1`; an optional reverse-proxy mount prefix is not part of the signature.
The timestamp must be within five minutes of server time. The query uses the
same external result contract as an API deployment. A POST delivery signature
cannot authorize a result query, and a query signature cannot be reused for
another invocation. Disabling admission preserves access to historical results.

## Native Linux

Requirements: cgroup v2 with CPU, memory and PIDs controllers, and a running
systemd host. The native bootstrap installs a delegated `flowork.service` for
the invoking unprivileged account and starts it. With `--prepare-only`, install
and start the service explicitly after editing `.env.launch.local`:

```bash
sudo .venv/bin/python scripts/install_native_service.py --user "$(id -un)"
sudo systemctl start flowork.service
```

To review the generated unit before installation:

```bash
.venv/bin/python scripts/install_native_service.py --user "$(id -un)" --print
```

The installer validates the unit with `systemd-analyze verify`, enables it at
boot and sets `Delegate=yes`. It does not restart an existing service. For an
upgrade, drain active work before `sudo systemctl restart flowork.service`.
Use `--repo`, `--launch-env` and `--unit` for a non-default installation. The
service reads operator settings from the launch environment file; secrets are
not copied into the unit. Manage this installation through systemctl so future
starts continue to receive the delegated cgroup environment. Direct
`./launch.sh start` remains a development entry point and does not provision
resource delegation.

The wrapper creates `supervisor` and `instances` children inside that service's
cgroup and exports `SANDBOX_CGROUP_ROOT` to its children. It refuses to move other
processes or operate on the host root cgroup. Do not point it at a login session
shared with unrelated applications. Launching directly without delegation cannot
start resource-limited resident deployments; there is no unlimited fallback.

The deployment pool defaults to 75% of available CPU and 50% of memory, bounded
by CPU affinity and ancestor cgroup limits. Operators can set:

| Variable | Meaning |
| --- | --- |
| `SANDBOX_DEPLOYMENT_CPU_MILLIS` | Total CPU budget for all deployment revisions |
| `SANDBOX_DEPLOYMENT_MEMORY_MB` | Total memory budget in MiB for all deployment revisions |
| `SANDBOX_MAX_RESIDENT` | Existing global resident-instance count limit, including other sandbox consumers |

Pool budgets must fit the host/ancestor limits. Leave capacity for the database,
API, background workers and interactive chats. A small host may not be able to
roll several deployments at once.

## Docker Compose

Only `sandboxd` owns cgroup control. The application image contains the wrapper;
Compose starts sandboxd through it, uses the host cgroup namespace and mounts
`/sys/fs/cgroup` read/write in this already privileged service. API, workers and
Web do not receive that mount. A Linux cgroup v2 host is required. Sandbox guests
must never receive a writable cgroup mount.

A freshly built sandbox image has passed the bounded kernel probe for
cgroup delegation, CPU throttling, memory OOM enforcement and overlapping-instance
admission. Its packaged API modules and real Bash PTY also pass the standalone
unprivileged check in `api/tests/container_terminal_probe.py`, without mounting
host application source. A clean Compose startup has also passed migration, Web/API health checks,
sandbox prewarm and delegated-resource checks. The temporary validation stack
is removed after verification.

The image defaults `VIBECANVAS_STORAGE_ROOT` to the writable
`/var/lib/vibecanvas/local-data` directory. Compose supplies its persistent
storage mounts and connection settings; direct image users must supply the
required database, public URL and authentication configuration.

## Small isolated kernel probe

`api/tests/cgroup_resource_probe.py` verifies real CPU throttling, memory OOM
enforcement, overlapping-instance capacity checks and final cgroup cleanup. Run
it inside a dedicated transient unit, never inside the live application unit:

```bash
sudo systemd-run --unit=flowork-cgroup-probe --collect --wait --pipe \
  --property=User=flowork --property=Delegate=yes --property=MemoryMax=384M \
  --setenv=PYTHONPATH=/opt/flowork/api/src \
  /opt/flowork/.venv/bin/python /opt/flowork/scripts/with_cgroup_delegation.py \
  /opt/flowork/.venv/bin/python /opt/flowork/api/tests/cgroup_resource_probe.py
```

This test deliberately exceeds a **test instance's** 128 MiB limit. It does not
call workflows, modify the database, or start a second application stack.

References: [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html),
[Compose cgroup namespace](https://docs.docker.com/reference/compose-file/services/#cgroup).

## Deployment terminal

The Terminal tab opens Bash only after the user clicks **Connect terminal**.
It requires a valid browser session, an approved Origin and deployment update
permission. The API rechecks the session, permission and active revision every
five seconds. A revision switch ends the old connection; reconnect explicitly to
open a shell in the new serving instance. Queued and running workflows are not
cancelled by terminal disconnects.

The PTY runs inside the same resident sandbox and resource cgroup. It supports
resize, UTF-8 input, paste and Ctrl+C. Output is acknowledged after xterm renders
it; the server pauses reads at a 64 KiB outstanding window. Scrollback is bounded.
The supervisor allows at most four terminals per instance and reaps abandoned
connections after two minutes. There is no host-shell fallback.

Reverse proxies must forward WebSocket Upgrade/Connection headers for
`/api/v1/deployments/{id}/terminal` (under the configured API prefix), preserve
Origin and cookies, and allow long-lived connections. Use WSS behind HTTPS.
Credentials must never appear in the connection URL. Disconnecting does not
undo shell commands or filesystem changes; instance replacement discards
instance-local files according to the deployment's normal storage policy.

The Docker Web image forwards upgrades for all `/api/` routes. For a native
Nginx installation, configure the same behavior (a browser-only WebSocket
location does not cover deployment terminals):

```nginx
# In the http context, outside server blocks.
map $http_upgrade $flowork_connection_upgrade {
    default upgrade;
    '' '';
}

# Inside the HTTPS server block.
location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $flowork_connection_upgrade;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

Run `nginx -t` before reloading Nginx. Empty upgrade requests keep ordinary
HTTP connections reusable and preserve streaming responses.

## Fixed workflow versions

Tasks and deployments use a saved `vN.svM` snapshot. Creation forms and Task /
Deployment CLI commands require a complete version (`--version`); workflow
editing commands still use `--major`. Legacy HTTP selectors (`head` or `major`)
are accepted for compatibility but resolved once at submission and stored as a
specific pin. Workflow edits never automatically upgrade these resources.
Incomplete explicit deployment pins are rejected instead of selecting HEAD.

Detail-page links open `/preview?type=workflow&workflowId=…&version=vN.svM`,
the isolated read-only saved canvas. The historical editor route remains an
editing surface and is not used for resource version links. Deployment version
fields show the clickable version number, without an extra action button.
Batch links use the frozen submission snapshot. Scheduled execution
links use the selected execution's snapshot, which can differ from the current
schedule configuration after an explicit update. Deployment links distinguish
its serving revision from configured/preparing versions. Unknown versions never
fall back to the editable workflow or to its latest saved version.

Clicking a node opens the shared workflow node-property Inspector in read-only
mode. Execution/trace canvases also show that node's recorded inputs, outputs,
status and errors from the selected execution; nodes without events are labeled
accordingly. A version-only preview has no execution attached and never reads
results from the live editor's run stores. Approval controls on execution nodes
retain their existing authorization and behavior.

Before upgrading an existing installation, back up its database and stop API,
worker and sandboxd processes. With the normal privileged database configuration,
run `python api/scripts/pin_resource_versions.py` to preview legacy resources,
then repeat with `--apply` before restarting. The operation is transactional and
idempotent; a missing workflow/version aborts it without committing partial
changes. It uses the encrypted schedule repository, leaves execution snapshots
unchanged and preserves a serving deployment revision when it matches the saved
configuration. Without such a revision, it resolves the configured selector once.
Keep the ID/version report with the private deployment backup.


### Detail-page information layout

Deployment Overview owns runtime instances and monitoring; hourly metric rows
are expandable below charts. Usage owns the endpoint, code examples and test
request. Activity prioritizes workflow traces and approvals; lower-level request
records remain expandable for source, timing and transport errors. Settings owns
identity, traffic/runtime controls and secret rotation. Old `monitoring`, `code`,
`test`, `runs`, `config` and `security` links retain useful destinations.

Task Overview owns progress or terminal outcome (one set of batch counters).
Configuration owns frozen batch inputs or schedule settings. Execution logs own
traces, expandable schedule triggers and raw events. Evaluation remains separate.
Version links resolve exact stored versions. Read-only workflow inspectors have
Node and Run info tabs; Run info reads only the selected trace, never the editor's
live execution store. Pure version previews explicitly have no attached run.

Skill and Knowledge detail pages share Files and Overview; file operations save
drafts and a separate confirmation publishes a version. MCP details use Overview,
Tools and Connection & settings, with technical identity/JSON collapsed.

`flowork-cli skill refresh --skill-id ID` refreshes one Skill in the active Chat's
read-only `/skills` namespace without remounting or restarting it. It resolves the
latest authorized immutable revision, stages files on the host, replaces that
folder and removes obsolete files. `skill publish --skill-id ID --source-dir DIR`
updates the platform package. `refresh` accepts no source directory.
Read the returned `runtime_path/SKILL.md` again after refresh. New Agent turns
already hydrate published Skills automatically.


Validation on the native deployment (2026-10-02): migration 154 was applied
before restarting API, worker and sandboxd. With the test account, real API and
browser checks covered Skill/Knowledge draft isolation, stale-write rejection,
required-root protection, file create/edit/replace/delete, unsaved-change
confirmation, publication and immutable historical reads. A live Agent in one
continuous turn published a QA Skill, verified that its runtime copy remained
old, explicitly refreshed it, verified new bytes and removal of obsolete files,
and confirmed the mount was still read-only. Task, scheduled Task, Deployment,
MCP and Skill tabs were inspected; successful and failed traces displayed
separate node and run information. The inspected pages produced no JavaScript
page errors. Desktop and narrow-screen layouts were checked without load tests.
Private test fixtures, credentials, screenshots and deployment backups remain
outside the repository.

## Invocation timeout

Deployment Settings and CLI create/update expose `timeout_seconds` (integer 1–3600, default 30). Each invocation snapshots this value at admission; editing the setting affects new calls without restarting the instance. Ordinary execution consumes the budget; Human approval waiting pauses it and uses the node timeout. The budget resumes after approval resolves. Explicit asynchronous calls use the same execution budget.

A synchronous call returns HTTP 202 only after its actual path encounters Human approval. Without approval, expiry cancels that invocation without terminating its shared worker before recording `timed_out` and returning HTTP 504 with `execution_timeout`. A queued, unclaimed call is fenced from late dispatch before timeout is recorded. Existing side effects are not rolled back. Result queries retain the same execution ID and terminal status; an HTTP disconnect alone is not an execution timeout.

Timeout cancels the affected invocation and stops its owned Code/Bash children. The shared RPC worker, resident Deployment session and sibling invocations remain available. Capacity is released only after that invocation has stopped. If a worker actually crashes, the pool replaces it for new calls; missing workers alone do not release the sandbox or replay lost calls.

Native-service acceptance (2026-10-04) verified HTTP 504 on an ordinary call timeout followed by HTTP 200 on the next call with unchanged resident worker PIDs, including unlimited-concurrency mode. A sibling Human approval call remained isolated from another call's timeout. Approval deadlines now terminate their own invocation with approval_timeout, as verified below. A 32-second ordinary call remained synchronous with a 45-second budget. Explicit asynchronous execution timed out durably and the next call succeeded. The Settings control rejected zero and persisted a changed timeout across reload without changing the active revision. Private credentials and test evidence remain outside the repository.


## Shared worker and persistent directory acceptance (2026-10-04)

Real-service checks covered CPU-based worker defaults, two resident workers,
explicit 2 × 2 admission (four accepted, overflow HTTP 429), changing to unlimited
concurrency (six overlapping successful calls), timeout HTTP 504 followed by a
successful call without changing worker PIDs, file visibility during revision
replacement, durable file restoration after disable/enable, and durable deletion.
The same invocation's inputs and node events remained available from history;
the Deployment directory was readable through authorized VFS access. QA
Deployments were disabled after acceptance.

Regression coverage also verifies least-occupied routing after reservations
are released, 40 simultaneous asynchronous approval waits in a single unlimited
worker, independent cancellation, and file writeback failure retaining its local
source for retry. Shared storage is intentionally not an invocation snapshot.


## Durable invocation trace verification (2026-10-04)

`deployment_invocations` stores the operational call summary. Full, encrypted
execution evidence lives in `workflow_execution_runs` (frozen graph and exact
version, invocation inputs, status and final result), `workflow_execution_events`
(ordered node events), and `workflow_execution_approvals` (approval decisions).
Trace storage is separate from the shared Deployment `/run`; deleting or
rebuilding a sandbox does not remove those database records.

Node-start events now include a snapshot of resolved inputs, so an interrupted
node still has inspectable inputs even though it never produced an output.
Successful and failed node events retain their output or error. An execution
timeout remains an execution-level terminal status; unfinished nodes are not
invented as successful or given fabricated outputs.

Live verification read 19 earlier calls after an API restart, checked 18 successful
calls for exact node-input/result correspondence and distinct trace identities,
and identified the missing interrupted-node inputs. After the fix, separate
real calls verified success (HTTP 200), a Code exception (HTTP 502), and timeout
(HTTP 504), including resolved inputs and errors. All three complete event
streams remained identical after disabling the Deployment and releasing its
sandbox. The all-node workload additionally exercises loop visits and parallel
spans; each event is bound to its own invocation and ordered sequence.

A synchronous response now waits for the invocation's operational completion,
including worker capacity release and file writeback. Previously, committed
terminal history could return before capacity was released, causing a client's
next in-limit call to receive an unexpected HTTP 429. Idempotent synchronous
replays observe the same completion boundary.

A load-test SubAgent failed because its delegated task prohibited all tools and
the model omitted `set_output`. Its input, error and actual model messages were
retained in the database. The system completion contract now explicitly says
that this prohibition covers optional business tools, while `set_output` remains
mandatory. No extra corrective conversation turn or automatic business replay
was added; prompt clarification cannot guarantee model compliance.


Final bounded comparison reused the existing 25-node, 15-type Workflow with
Human approval removed, on 500 millicores / 256 MiB and one worker with an
explicit four-invocation limit:

| Client concurrency | Verified calls | Instance peak MiB | Observed calls/s |
| --- | --- | --- | --- |
| 1 | 2/2 | 141.7 | 0.125 |
| 2 | 4/4 | 156.3 | 0.397 |
| 4 | 8/8 | 186.6 | 0.664 |

All final calls returned HTTP 200 with validated business outputs; no OOM,
worker replacement or unexpected HTTP 429 occurred. All 798 persisted events,
392 node visits and 15 node types were checked for exact version, invocation
identity, ordered sequence, resolved inputs, outputs, loop visits and parallel
parent spans after the test Deployment was disabled. Earlier separate-process
measurements reached about 252 MiB at concurrency two and OOM at concurrency
four. These are short samples, include a cold first call and model latency, and
do not establish a maximum sustainable QPS. The earlier capacity-release failure
and model completion failure remain recorded as findings, not erased by the
successful final run.


## Approval deadline and read-only preview acceptance (2026-10-04)

The real service verified approval timeout through canvas execution, two batch
rows with one execution slot, a one-time scheduled task, and an API Deployment.
Each timed-out execution retained its node inputs and error, had no downstream
EndNode event or business output, and stored approval status `timeout` with
`approved=null`, execution status `timed_out` and error `approval_timeout`. Late
approval attempts returned HTTP 409. Batch processing advanced to the next row.

Two calls shared one resident Deployment worker: one reached approval timeout
while the other was explicitly approved and succeeded. A later explicit rejection
continued with `approved=false`; cancellation produced no decision. Worker PIDs
were unchanged. The original asynchronous ticket remained queryable with HTTP
200 and its terminal `approval_timeout` payload. The QA Deployment was disabled
and the one-time schedule paused after verification.

Version previews now apply presentation-only auto layout before display and
again when measured node sizes arrive. They do not persist positions or change
the selected Workflow version. Task and Deployment version links carry their
source route (including the selected tab); preview Back and Close navigate to
that source without depending on browser permission to close a tab. Old links
use in-app browser history when available, otherwise a conversation/home exit.
Execution trace pages also provide an explicit link back to their owning task,
deployment or workflow.

Eight real Chromium browser cases at 390px and 1440px widths covered the
reported overlapping Workflow, batch and scheduled task version previews,
non-overlapping node bounds, read-only Node/Run info inspectors, reload/direct
entry followed by Back/Close, and approval-timeout trace messages with no active
approval buttons. All passed without page errors; the stored snapshot remained
unchanged. This is mobile-viewport coverage, not a physical Safari-device test.


### Execution state notifications

HTTP observers and sandbox execution drivers subscribe to PostgreSQL
`flowork_execution_state` notifications.
State-change triggers publish only execution IDs at transaction commit. The API
shares one listener per active event loop and uses tenant-bound pooled sessions
to read results on events; normal waiting does not poll execution tables. A
separate deadline wakes synchronous calls for timeout handling. Reconnecting the
listener prompts a state read to recover missed events.

`STATE_NOTIFICATION_DATABASE_URL` may point to the same application database
through a direct or session-pooled connection when `DATABASE_URL` uses transaction
pooling. It defaults to `DATABASE_URL`; PostgreSQL LISTEN must not pass through a
transaction-mode PgBouncer pool. No additional service is required.

The sandbox driver waits for runtime events or a committed approval/cancel/timeout
command. Empty runtime heartbeat replies do not read or lock execution tables.
Runtime transport waits remain bounded (25 seconds) to detect lost connections;
this heartbeat does not decide execution status or trigger state polling. Only
uncertain command delivery retries briefly while the transport is failing.
