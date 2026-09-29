# Resident deployment resources

Each deployment revision has its own resident bubblewrap instance. Its resource
budget covers the instance's descendants, including concurrent workflow workers
and terminal processes. A limit is not a reservation of physical RAM.
The 256 MiB starting size leaves room for rolling overlap on small hosts;
increase it for workflows with larger in-memory datasets.

## Configuration changes and rollout

The Settings page shows a confirmation with the previous and proposed values
before every save. Cancelling leaves the draft available for editing.

| Change | Effect after confirmation |
| --- | --- |
| QPS | Updates admission limits without replacing the instance |
| Workflow version, its dependencies, or `/mount` | Prepares a replacement instance |
| CPU or memory | Prepares a replacement with the new limits |
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
and running calls stay on the old revision. It is removed only after those calls
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
terminal invocation. Persisted result summaries contain counts, not outputs.
Queued invocations remain bound to their admitted revision until execution.

Execution ownership is claimed atomically in PostgreSQL. The executor renews a
60-second lease every 10 seconds; failure to renew cancels only that request's
worker. The controller marks expired executions as failed, and local worker
leases plus cgroup emptiness checks still prevent premature instance removal.
This also releases durable drain blockers after a daemon is killed without
running its finalizers. Expired or completed calls are not automatically
re-executed, because their external side effects may already have happened.

Synchronous API and dashboard test calls have five minutes to reach the
executor after admission. Unclaimed calls past that deadline fail with
`dispatch_expired`. Background calls that are still queued have no dispatch
expiry; their execution lease starts only when sandboxd claims them. Apply
migration 144 before running this version, and drain active calls during an
upgrade from an older executor that does not renew leases.

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
