"""Host-side resident Chat Agent sandbox.

A process singleton (:class:`SandboxManager`) maps ``(tenant_id, wf_id)`` → a
resident :class:`SandboxSession`. The session wraps the shipped rootless-gVisor
provider plus the per-chat and user mounts, runs agent Skill scripts through
``RootlessGvisorProvider.run_code``, and writes the run dir's VFS folders back to
the durable VFS after each run.

Resident model (``config.sandbox_resident_mode == "coldboot"``, the dev/default):
legacy ``run_code`` cold-boots a one-shot bundle against persisted workspace
mounts. Main Agent turns and agent-visible shell/file tools use separate warm
gVisor workers over those same mounts, so subsequent turns avoid container and
Python import cold-start while all tools share one filesystem view.

Concurrency: a per-session ``asyncio.Lock`` serializes legacy ``run_code`` skill
scripts. Agent-visible shell/file tools use one warm gVisor sandbox with an
in-sandbox parallel job server, so multiple commands can run over the same
mounted workspace with normal filesystem semantics. The manager bounds the
resident fleet at ``max_resident`` and evicts the least-recently-used session on
overflow; idle sessions past ``idle_ttl_s`` are reaped by
:meth:`SandboxManager.sweep_idle`. The provider methods are SYNC, so they run
via ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid

import structlog
from vibecanvas_engine.sandbox_bus import (
    MSG_RUNTIME_CONTROL,
    MSG_RUNTIME_ERROR,
    MSG_RUNTIME_EVENT,
    MSG_RUNTIME_REQUEST,
    MSG_RUNTIME_RESULT,
)

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.codex_account import (
    codex_account_auth_file,
)
from vibecanvas_api.services.codex_cli import (
    codex_cli_node_runtime,
    codex_cli_readonly_root,
    resolve_codex_executable,
)
from vibecanvas_api.services.env.overlay_builder import ensure_overlay
from vibecanvas_api.services.object_store import FilesystemObjectStore, get_object_store
from vibecanvas_api.services.run_workspace import RunWorkspace
from vibecanvas_api.services.sandbox import get_sandbox_provider
from vibecanvas_api.services.sandbox.admission import sandbox_admission
from vibecanvas_api.services.sandbox.bus_broker import BusBroker, socket_path_for
from vibecanvas_api.services.sandbox.gvisor import ServeSnapshot, _workflow_python_binds
from vibecanvas_api.services.sandbox.session_lifecycle import (
    SessionLifecycleState,
    SnapshotKind,
    validate_lifecycle_transition,
)
from vibecanvas_api.services.sandbox.snapshot_store import (
    snapshot_category_root,
    snapshot_entries,
    snapshot_tree_bytes,
)
from vibecanvas_api.services.sandbox.warm import WarmGvisorPool
from vibecanvas_api.services.user_mount_workspace import (
    hydrate_user_mount,
    persist_user_mount,
    remove_user_mount,
)
from vibecanvas_api.services.user_mount_workspace import (
    mount_scope_id as user_mount_scope_id,
)
from vibecanvas_api.services.vfs_run_context import (
    build_run_context,
    sync_run_back,
)
from vibecanvas_api.services.vfs_volume import (
    ChatRuntimeVolume,
    get_chat_runtime_volume_provider,
)
from vibecanvas_api.storage.db import session_scope, short_session_scope
from vibecanvas_api.storage.vfs_store import VfsRepo

logger = structlog.get_logger(__name__)
_SNAPSHOT_STORE_LOCK = asyncio.Lock()

# The Chat workspace folders written back to durable VFS — agent working area
# (/data), scratch memory (/memory), and run logs (/logs). Each is a host
# subdir of the chat/workspace ``run_dir`` mirrored to the matching VFS prefix.
_RUN_WRITEBACK_FOLDERS = ("data", "memory", "logs")
_SANDBOX_BASELINE_TOOLS = (
    "git",
    "jq",
    "curl",
    "ssh",
    "rg",
    "zip",
    "unzip",
    "patch",
    "file",
    "ps",
    "tar",
    "node",
    "python",
)
_SANDBOX_BASELINE_PYTHON_MODULES = (
    "bs4",
    "docx",
    "httpx",
    "jsonlines",
    "lxml",
    "markdown",
    "matplotlib",
    "networkx",
    "numpy",
    "openpyxl",
    "pandas",
    "PIL",
    "pptx",
    "pypdf",
    "reportlab",
    "requests",
    "seaborn",
    "tabulate",
    "xlsxwriter",
    "yaml",
)
DIR_KEEP_SENTINEL = ".vibekeep"


def _runtime_identity_component(value: str, *, field: str) -> str:
    normalized = str(value or "")
    if (
        not normalized
        or normalized in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9_.-]+", normalized) is None
    ):
        raise ValueError(f"invalid {field} for Runtime state path")
    return normalized


def _empty_leaf_dirs(root: str) -> list[str]:
    """Return empty leaf directories relative to ``root``."""
    out: list[str] = []
    for current, dirs, files in os.walk(root):
        if current != root and not dirs and not files:
            out.append(os.path.relpath(current, root).replace(os.sep, "/"))
    return out


def _fileop_should_resubmit(res: dict) -> bool:
    """Whether a warm fileop result is an infrastructure miss worth retrying.

    User errors (not_found/path_outside_roots/etc.) must surface immediately.
    Warm-pool transport errors that explicitly say "resubmit" are recoverable.
    The pool reports recoverable transport misses; the session retries the exact
    op once without forcing a sandbox rebuild.
    """
    if res.get("ok"):
        return False
    err = str(res.get("error") or "")
    return "resubmit" in err and (
        "while QUEUED" in err
        or ("worker" in err and "died" in err)
        or "worker busy" in err
    )


def _session_inflight_operations(session: object) -> int:
    """Return a real activity count while keeping lightweight test doubles idle."""
    value = getattr(session, "_inflight_operations", 0)
    return value if isinstance(value, int) else 0


def _session_lifecycle_state(session: object) -> str:
    """Read lifecycle state without treating loose test doubles as hibernated."""
    value = getattr(session, "_lifecycle_state", "warm")
    return value if isinstance(value, str) else "warm"


def _session_resource_status(session: object) -> dict[str, object]:
    """Project real sessions while keeping lightweight test doubles stable."""

    projector = getattr(session, "resource_status", None)
    if callable(projector):
        projected = projector()
        if isinstance(projected, dict):
            return projected
    return {
        "workspace_projection": "materialized",
        "vfs_mount": "unknown",
        "runtime_volume": "unknown",
        "runtime_process": "unknown",
        "authentication": "unknown",
        "network": "unknown",
        "snapshot_kind": None,
        "runtime_type": None,
        "lifecycle_generation": int(
            getattr(session, "_lifecycle_generation", 0) or 0
        ),
        "lifecycle_state": _session_lifecycle_state(session),
    }


def _released_resource_status(*, lifecycle_state: str) -> dict[str, object]:
    return {
        "workspace_projection": "released",
        "vfs_mount": "detached",
        "runtime_volume": "detached",
        "runtime_process": "stopped",
        "authentication": "detached",
        "network": "disconnected",
        "snapshot_kind": None,
        "runtime_type": None,
        "lifecycle_generation": None,
        "lifecycle_state": lifecycle_state,
    }


# FIXED installer wrapper (F2.5). Reads {"spec","manager"} from stdin JSON and
# runs the installer as an argv LIST — the package spec is NEVER interpolated
# into a shell command or the script source, so it can't break out (injection
# -safe). pip targets the per-wf overlay (/opt/agent-overlay/py, on PYTHONPATH);
# apt is best-effort. The script exits with the installer's return code.
_INSTALL_WRAPPER = r'''
import sys, json, subprocess
spec_in = json.load(sys.stdin)
spec = spec_in["spec"]
manager = spec_in.get("manager", "pip")
if manager == "apt":
    cmd = ["apt-get", "install", "-y", spec]
else:
    cmd = [sys.executable, "-m", "pip", "install", "--target", "/opt/agent-overlay/py", spec]
proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
sys.stdout.write(proc.stdout or "")
sys.stderr.write(proc.stderr or "")
sys.exit(proc.returncode)
'''


def _guess_ct(rel: str, data: bytes) -> str:
    """Content type for files synced from the Chat workspace."""
    from vibecanvas_api.services.vfs_run_context import _guess_ct as _g
    return _g(rel, data)


async def _hydrate_run_folders(run_dir: str, wf_id: str, tenant_id: str) -> int:
    """Hydrate ``{run_dir}/data|memory|logs`` from the durable VFS — the exact
    INVERSE of :meth:`SandboxSession._sync_run_folder`'s write-back.

    On every session (re)build the run-dir folders are created EMPTY; the durable
    truth lives in the wf's ``VfsArtifact`` rows under ``/{folder}/``. Without this
    an LRU evict + rebuild would lose the agent's prior ``/data`` files from the
    working FS (the rows persist for the Explorer, but the sandbox starts blank).

    Lists rows under each ``/{folder}/`` prefix and fetches bytes through the
    ``VfsRepo`` read path (object-store backed, InMemory-safe), and write them to
    ``{run_dir}/{folder}/{rel}`` (``rel`` = the path after ``/{folder}/``), creating
    parent dirs. ``.vibekeep`` sentinels are normal 0-byte rows → writing them
    recreates empty dirs for free (no special handling).

    Fail-soft per file AND per folder: a hydrate failure must never block session
    creation (logged, then skipped). The DB reads stay on the event loop (async
    session); the blocking ``open().write()`` runs off-loop via ``asyncio.to_thread``
    (matching how ``build_run_context`` is offloaded at the call site).

    Returns the count of files written (for tests / observability).
    """
    written = 0
    for folder in _RUN_WRITEBACK_FOLDERS:
        prefix = f"/{folder}/"
        sub = os.path.join(run_dir, folder)
        # One failed prefix query must not poison the transaction used by the
        # remaining folders. A missing/deleted logical scope is expected during
        # recovery and should hydrate as an empty workspace.
        try:
            async with session_scope(tenant_id=tenant_id) as s:
                repo = VfsRepo(s, object_store=get_object_store())
                entries = await repo.ls(wf_id=wf_id, prefix=prefix)
                payloads: list[tuple[str, bytes]] = []
                for entry in entries:
                    if not entry.path.startswith(prefix):
                        continue
                    relative = entry.path[len(prefix):]
                    parts = relative.split("/")
                    if (
                        not relative
                        or any(part in {"", ".", ".."} for part in parts)
                    ):
                        logger.warning(
                            "agent_hydrate_unsafe_path_skipped",
                            wf_id=wf_id,
                            folder=folder,
                            path=entry.path,
                        )
                        continue
                    data = await repo.read_bytes(wf_id=wf_id, path=entry.path)
                    if data is None:
                        continue
                    destination = os.path.join(sub, *parts)
                    if os.path.commonpath(
                        [os.path.realpath(sub), os.path.realpath(destination)]
                    ) != os.path.realpath(sub):
                        logger.warning(
                            "agent_hydrate_unsafe_path_skipped",
                            wf_id=wf_id,
                            folder=folder,
                            path=entry.path,
                        )
                        continue
                    payloads.append((destination, data))
        except Exception:  # fail-soft per folder and transaction
            logger.warning(
                "agent_hydrate_folder_failed",
                wf_id=wf_id,
                folder=folder,
                exc_info=True,
            )
            continue

        def _flush(items: list[tuple[str, bytes]]) -> int:
            count = 0
            for destination, data in items:
                try:
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    with open(destination, "wb") as file:
                        file.write(data)
                    count += 1
                except OSError:  # fail-soft per file
                    logger.warning(
                        "agent_hydrate_file_write_failed",
                        wf_id=wf_id,
                        folder=folder,
                        dest=destination,
                        exc_info=True,
                    )
            return count

        if payloads:
            written += await asyncio.to_thread(_flush, payloads)
    return written


_EDIT_DIFF_MAX_LINES = 200
_HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _edit_unified_diff(before: str, after: str, path: str) -> str:
    """A compact hunk diff annotated with real file line numbers.

    Rows are ``[line] [marker]<TAB>[content]``:
    - unchanged rows have a blank marker and use the new-file line number.
    - ``-`` rows use the original-file line number.
    - ``+`` rows use the new-file line number.

    The tab is the hard boundary; content after it is the file text. Capped at
    ``_EDIT_DIFF_MAX_LINES`` so a huge replace_all cannot blow up context.
    """
    out: list[str] = []
    old_ln = 0
    new_ln = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                     lineterm="", n=3):
        if line.startswith("---") or line.startswith("+++"):
            continue                                   # drop file headers (path is in the summary)
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                old_ln = int(m.group(1))
                new_ln = int(m.group(2))
            out.append(line)
        elif line.startswith("-"):
            out.append(f"{old_ln:>4} -\t{line[1:]}")
            old_ln += 1
        elif line.startswith("+"):
            out.append(f"{new_ln:>4} +\t{line[1:]}")
            new_ln += 1
        else:                                          # context (leading space)
            out.append(f"{new_ln:>4}  \t{line[1:]}")
            old_ln += 1
            new_ln += 1
    if len(out) > _EDIT_DIFF_MAX_LINES:
        extra = len(out) - _EDIT_DIFF_MAX_LINES
        out = out[:_EDIT_DIFF_MAX_LINES] + [f"… [diff truncated — {extra} more lines]"]
    return "\n".join(out)


class SandboxSession:
    """One resident Chat sandbox: provider + persisted mounts + lock.

    Materialized once by :meth:`SandboxManager._build_session`; ``run_code`` is
    serialized by ``self._lock`` and cold-boots a fresh gVisor bundle against the
    persisted Chat workspace, user mount, and package overlay.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        wf_id: str,
        run_dir: str | None,
        overlay_dir: str | None,
        provider,
        base_binds: list[str],
        mount_dir: str | None = None,
        runtime_dir: str | None = None,
        runtime_volume: ChatRuntimeVolume | None = None,
        account_auth_file: str | None = None,
        skills_dir: str | None = None,
        mount_scope_id: str | None = None,
        user_id: str | None = None,
        expose_run: bool = True,
        lease: str = "interactive",
        pool_runs_root: str | None = None,
        materialized_projection_root: str | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.wf_id = wf_id
        # Chat/workspace-owned host dir. It backs /data, /memory, and /logs.
        self.run_dir = run_dir
        # `/run` is temporary execution space inside this Chat sandbox. It is not
        # selected-Workflow state and never changes when Workflow binding changes.
        self.workflow_run_dir = run_dir if expose_run else None
        self.workflow_run_id = wf_id if expose_run else None
        self.lease = lease if lease in {"interactive", "resident"} else "interactive"
        self.overlay_dir = overlay_dir
        self.provider = provider
        # User-level shared storage bound at in-sandbox ``/mount``. This is
        # independent from the chat workspace and the selected workflow.
        self.mount_dir = mount_dir
        # Chat-scoped private Runtime volume. It is mounted directly for the
        # in-sandbox runtime, but excluded from file-tool roots and Object Store
        # writeback. Closing a sandbox never deletes or copies this directory.
        self.runtime_dir = runtime_dir
        self.runtime_volume = runtime_volume
        self.account_auth_file = account_auth_file
        self.skills_dir = skills_dir
        self.mount_scope_id = mount_scope_id
        self.user_id = user_id
        self.base_binds = base_binds
        self.expose_run = expose_run
        self.pool_runs_root = pool_runs_root
        self.materialized_projection_root = materialized_projection_root
        # Writable binds layered into EVERY run of this session:
        #   - the per-wf pip overlay at ``/opt/agent-overlay`` (PYTHONPATH'd by
        #     the provider so installed packages import); ``None`` → no overlay.
        #   - TOP-LEVEL ``/data`` ``/memory`` ``/logs`` as the only user-visible
        #     workspace roots for those folders. The host storage layout remains
        #     under ``run_dir/{folder}``, but ``/run`` is not exposed for pure Chat.
        self._rw_binds: list[tuple[str, str]] = (
            [("/opt/agent-overlay", overlay_dir)] if overlay_dir else []
        )
        if run_dir:
            for _f in _RUN_WRITEBACK_FOLDERS:
                self._rw_binds.append((f"/{_f}", os.path.join(run_dir, _f)))
        if mount_dir:
            self._rw_binds.append(("/mount", mount_dir))
        if runtime_dir:
            self._rw_binds.append(("/runtime", runtime_dir))
        self._lock = asyncio.Lock()
        # Lifecycle I/O (checkpoint/restore/release) is serialized separately
        # from ordinary execution. SandboxManager may therefore wait for one
        # Chat to restore without holding its process-global registry lock or
        # blocking lifecycle transitions for unrelated Chats.
        self._transition_lock = asyncio.Lock()
        self._sandbox_runtime_id = f"chat-sandbox:{uuid.uuid4()}"
        # This epoch advances whenever the main Agent Runtime process is
        # replaced, even if sandboxd and the logical Chat session stay alive.
        # Host Gateway calls will fence identities against this value.
        self._runtime_process_generation = 0
        self._lifecycle_generation = 0
        # One duplex control channel per active Agent Turn. The runtime process
        # may wait for HITL for an arbitrarily long time while the frontend and
        # API requests reconnect; control traffic must therefore not depend on
        # the original SSE response object.
        self._runtime_brokers: dict[str, BusBroker] = {}
        self._runtime_broker_lock = asyncio.Lock()
        # Main-Agent Runtime process, kept warm across turns for this Chat.
        # A session is already serialized by ``_lock``, so one duplex channel
        # can safely carry consecutive requests while HITL control messages are
        # still routed by the active turn id through ``_runtime_brokers``.
        self._runtime_broker: BusBroker | None = None
        self._runtime_handle = None
        self._runtime_type: str | None = None
        self._runtime_uses_codex_account = False
        # Unlike the two fields above, the binding survives Runtime process
        # teardown. Account disconnect must invalidate a hibernated Chat too,
        # not only a currently live app-server process.
        self._bound_runtime_type: str | None = None
        self._bound_runtime_uses_codex_account = False
        self.last_used = time.monotonic()
        self._inflight_operations = 0
        self.closed = False
        # Task 4b-ii — the warm-backed file API. A no-DB worker whose network
        # posture follows SANDBOX_NETWORK (dev default: host).
        # ``WarmGvisorPool`` whose worker mounts THIS session's clean file roots
        # (/data /memory /logs /mount) and serves the agent's file ops IN the
        # sandbox. Lazily built ONCE on first file op (``_get_fileop_pool``),
        # under ``_fileop_lock`` (a dedicated lock so a file op never contends
        # with ``run_code``/writeback on ``self._lock``). ``None`` until built,
        # and stays ``None`` when no sandbox is possible (``run_dir is None``).
        self._fileop_pool: WarmGvisorPool | None = None
        self._fileop_lock = asyncio.Lock()
        # Workflow-only incremental Python environment. It is initialized by
        # node/workflow execution immediately before this session's first job,
        # never by ordinary Chat/runtime/file-preview startup. A settings
        # revision selects a new immutable content-addressed layer once.
        self._workflow_dependency_lock = asyncio.Lock()
        self._workflow_dependency_key: str | None = None
        self._workflow_dependency_pythonpath: str | None = None
        # Staging files and the fixed __exec__ channel belong to sandboxd. This
        # lock prevents requests arriving through different API workers from
        # racing on the same resident Workflow session.
        self._workflow_job_lock = asyncio.Lock()
        # Turn-end async writeback (G3): one background diff-writeback task at a
        # time per session, with a single coalesced re-run. ``schedule_writeback``
        # starts it; ``drain_writeback`` awaits it before teardown.
        self._wb_task: asyncio.Task | None = None
        self._wb_pending = False
        # Canonical files promoted by a semantic host-side commit must never be
        # written back through the generic last-writer-wins turn boundary.
        self._external_vfs_fenced_paths: set[str] = set()
        # Separate from the session execution lock: Platform MCP tools can run
        # while the Runtime owns ``_lock``. This lock only serializes the
        # writeback fence check with its durable VFS upsert.
        self._external_vfs_lock = asyncio.Lock()
        self._requires_rehydrate = False
        # Interactive Chat and Workflow-page Debug sessions share this state
        # machine when SANDBOX_TYPE=rootful-snapshot. Connected Runtime sockets
        # are deliberately not checkpointed; gVisor restores them reset. The
        # credential-free file/workflow worker is the only snapshotted process.
        self._lifecycle_state = SessionLifecycleState.WARM.value
        self._hibernated_at: float | None = None
        self._serve_snapshot: ServeSnapshot | None = None
        self._snapshot_dir: str | None = None
        self._snapshot_error: str | None = None
        self._last_activity_sequence: int | None = None
        self._activity_was_busy = False

    def _transition_lifecycle(
        self,
        target: SessionLifecycleState | str,
    ) -> None:
        """Advance the Runtime-neutral state and its fencing generation."""

        _source, destination = validate_lifecycle_transition(
            self._lifecycle_state,
            target,
        )
        if destination.value == self._lifecycle_state:
            return
        self._lifecycle_state = destination.value
        self._lifecycle_generation += 1

    def resource_status(self) -> dict[str, object]:
        """Return a secret-free projection of mounts and security resources."""

        state = _session_lifecycle_state(self)
        handle = self._runtime_handle
        runtime_live = bool(
            handle is not None
            and getattr(handle, "proc", None) is not None
            and handle.proc.poll() is None
        )
        pool = self._fileop_pool
        fileop_live = bool(pool is not None and getattr(pool, "_handles", None))
        if runtime_live and self._runtime_uses_codex_account:
            authentication = "account_bound"
        elif runtime_live and self._runtime_brokers:
            authentication = "turn_capability_active"
        else:
            authentication = "detached"
        snapshot = self._serve_snapshot
        return {
            "workspace_projection": (
                "materialized" if self.run_dir and not self.closed else "released"
            ),
            "vfs_mount": "attached" if fileop_live or runtime_live else "detached",
            "runtime_volume": (
                "attached"
                if self.runtime_dir and runtime_live
                else "detached"
                if self.runtime_dir
                else "not_required"
            ),
            "runtime_process": "resident" if runtime_live else "stopped",
            "authentication": authentication,
            "network": "connected" if runtime_live else "disconnected",
            "snapshot_kind": (
                str(snapshot.kind) if snapshot is not None else None
            ),
            "runtime_type": self._bound_runtime_type,
            "lifecycle_generation": self._lifecycle_generation,
            "lifecycle_state": state,
        }

    def observe_activity(self, *, now: float | None = None) -> dict[str, object]:
        """Observe host leases plus the sandbox-published positive activity.

        The guest state may extend a lease but is never trusted to shorten one.
        A changed activity sequence while currently idle means work began and
        ended between daemon polls, so the silent clock starts at observation.
        """
        observed_at = time.monotonic() if now is None else now
        inflight = _session_inflight_operations(self)
        runtime_brokers = getattr(self, "_runtime_brokers", {})
        broker_count = len(runtime_brokers)
        wb_task = getattr(self, "_wb_task", None)
        writeback_busy = bool(
            getattr(self, "_wb_pending", False)
            or (wb_task is not None and not wb_task.done())
        )
        pool_state: dict[str, object] = {}
        pool = getattr(self, "_fileop_pool", None)
        if pool is not None and _session_lifecycle_state(self) == "warm":
            pool_state = pool.activity_snapshot()
        guest_busy = any(
            int(pool_state.get(name) or 0) > 0
            for name in (
                "queued_jobs",
                "claimed_jobs",
                "active_markers",
                "reported_active_jobs",
                "abandoned_jobs",
            )
        )
        busy = bool(inflight or broker_count or writeback_busy or guest_busy)
        sequence_value = pool_state.get("activity_sequence")
        sequence = int(sequence_value) if isinstance(sequence_value, int) else None
        sequence_advanced = bool(
            sequence is not None
            and getattr(self, "_last_activity_sequence", None) is not None
            and sequence != self._last_activity_sequence
        )
        if busy or getattr(self, "_activity_was_busy", False) or sequence_advanced:
            self.last_used = observed_at
        if sequence is not None:
            self._last_activity_sequence = sequence
        self._activity_was_busy = busy
        return {
            "busy": busy,
            "inflight_operations": inflight,
            "broker_count": broker_count,
            "writeback_busy": writeback_busy,
            "sandbox": pool_state,
        }

    @staticmethod
    def _snapshot_store_root(
        kind: SnapshotKind | str = SnapshotKind.SESSION_HIBERNATION,
    ) -> str:
        return snapshot_category_root(kind)

    @staticmethod
    def _tree_bytes(root: str) -> int:
        total = 0
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if not os.path.islink(os.path.join(current, name))
            ]
            for name in files:
                path = os.path.join(current, name)
                if not os.path.islink(path):
                    total += os.lstat(path).st_size
        return total

    @staticmethod
    def _remove_snapshot_tree(path: str | None) -> None:
        if not path:
            return
        root = os.path.realpath(config.sandbox_snapshot_root)
        candidate = os.path.abspath(path)
        if os.path.commonpath((root, candidate)) != root or candidate == root:
            raise ValueError("snapshot cleanup path escapes SANDBOX_SNAPSHOT_ROOT")
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("snapshot cleanup target must be a real directory")
        shutil.rmtree(candidate)

    def _snapshot_fingerprint(self) -> str:
        pool = self._fileop_pool
        payload = {
            "format": 2,
            "kind": SnapshotKind.SESSION_HIBERNATION.value,
            "python": sys.version,
            "tenant_scope": hashlib.sha256(self.tenant_id.encode()).hexdigest(),
            "session_scope": hashlib.sha256(self.wf_id.encode()).hexdigest(),
            "rw": sorted((str(dest), os.path.abspath(src)) for dest, src in self._rw_binds),
            "ro": sorted(map(os.path.abspath, self.base_binds)),
            "workers": int(pool.size) if pool is not None else 0,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def hibernate(self) -> bool:
        """Checkpoint an idle interactive session and release its live memory."""
        async with self._transition_lock:
            return await self._hibernate_once()

    async def _hibernate_once(self) -> bool:
        """Perform one hibernation while holding ``_transition_lock``."""
        if config.sandbox_resident_mode != "snapshot":
            return False
        if _session_inflight_operations(self) != 0:
            return False
        await self.drain_writeback()
        async with self._lock:
            if self.closed or self._lifecycle_state == "hibernated":
                return False
            if _session_inflight_operations(self) != 0:
                return False
            self._transition_lifecycle(SessionLifecycleState.HIBERNATING)
            try:
                pool = self._fileop_pool
                if pool is not None and not await asyncio.to_thread(pool.is_quiescent):
                    # Activity that outlived a host waiter is not a snapshot
                    # failure. Return to Warm and begin the idle clock only
                    # after sandboxd observes the eventual idle transition.
                    self._transition_lifecycle(SessionLifecycleState.WARM)
                    self.last_used = time.monotonic()
                    self._activity_was_busy = True
                    return False
                await self.writeback_vfs()
                # Host-network and host-UDS connections are reset by restore.
                # Persist Runtime-owned state, then close it cleanly instead of
                # publishing a snapshot whose broker is already disconnected.
                await self._stop_agent_runtime_locked()
                if self.runtime_volume is not None:
                    await asyncio.to_thread(
                        get_chat_runtime_volume_provider().sync,
                        self.runtime_volume,
                    )

                old_snapshot_dir = self._snapshot_dir
                new_snapshot_dir: str | None = None
                snapshot: ServeSnapshot | None = None
                if pool is not None:
                    if not await asyncio.to_thread(pool.is_quiescent):
                        self._transition_lifecycle(SessionLifecycleState.WARM)
                        self.last_used = time.monotonic()
                        self._activity_was_busy = True
                        return False
                    # Never hold a synchronous lock across checkpoint I/O: an
                    # explicit checkpoint for another session must not block
                    # the sandboxd event loop.
                    async with _SNAPSHOT_STORE_LOCK:
                        root = self._snapshot_store_root(
                            SnapshotKind.SESSION_HIBERNATION
                        )
                        entries = snapshot_entries()
                        if len(entries) >= config.sandbox_snapshot_max_count:
                            raise RuntimeError("sandbox snapshot count limit reached")
                        scope_digest = hashlib.sha256(
                            f"{self.tenant_id}\0{self.wf_id}".encode()
                        ).hexdigest()[:16]
                        new_snapshot_dir = tempfile.mkdtemp(
                            prefix=f"session-{scope_digest}-", dir=root
                        )
                        os.chmod(new_snapshot_dir, 0o700)
                        image_dir = os.path.join(new_snapshot_dir, "image")
                        snapshot = await asyncio.to_thread(
                            pool.checkpoint,
                            image_dir=image_dir,
                            fingerprint=self._snapshot_fingerprint(),
                            kind=SnapshotKind.SESSION_HIBERNATION.value,
                        )
                        total_bytes = snapshot_tree_bytes(entries) + self._tree_bytes(
                            new_snapshot_dir
                        )
                        if total_bytes > config.sandbox_snapshot_max_bytes:
                            self._remove_snapshot_tree(new_snapshot_dir)
                            raise RuntimeError("sandbox snapshot byte limit reached")
                self._serve_snapshot = snapshot
                self._snapshot_dir = new_snapshot_dir
                self._hibernated_at = time.monotonic()
                self._snapshot_error = None
                self._transition_lifecycle(SessionLifecycleState.HIBERNATED)
                if old_snapshot_dir and old_snapshot_dir != new_snapshot_dir:
                    await asyncio.to_thread(
                        self._remove_snapshot_tree, old_snapshot_dir
                    )
                logger.info("sandbox_session_hibernated", wf_id=self.wf_id)
                return True
            except Exception as exc:
                self._snapshot_error = type(exc).__name__
                self._transition_lifecycle(SessionLifecycleState.SNAPSHOT_FAILED)
                logger.exception(
                    "sandbox_session_hibernate_failed",
                    wf_id=self.wf_id,
                    failure=self._snapshot_error,
                )
                raise

    async def resume(self) -> bool:
        """Restore a hibernated session before accepting another operation."""
        async with self._transition_lock:
            return await self._resume_once()

    async def _resume_once(self) -> bool:
        """Perform one restore while holding ``_transition_lock``."""
        async with self._lock:
            if self._lifecycle_state == "warm":
                return False
            if self._lifecycle_state == "snapshot_failed":
                raise RuntimeError(
                    "sandbox snapshot failed; explicitly close the session before retrying"
                )
            if self._lifecycle_state != "hibernated":
                raise RuntimeError(f"sandbox session is {self._lifecycle_state}")
            self._transition_lifecycle(SessionLifecycleState.RESTORING)
            try:
                if self._serve_snapshot is not None:
                    pool = self._fileop_pool
                    if pool is None:
                        raise RuntimeError("sandbox snapshot has no owning worker pool")
                    if (
                        self._serve_snapshot.kind
                        != SnapshotKind.SESSION_HIBERNATION.value
                    ):
                        raise RuntimeError("sandbox session snapshot kind mismatch")
                    if self._serve_snapshot.fingerprint != self._snapshot_fingerprint():
                        raise RuntimeError("sandbox session snapshot fingerprint mismatch")
                    await asyncio.to_thread(pool.restore, self._serve_snapshot)
                self._transition_lifecycle(SessionLifecycleState.WARM)
                self._hibernated_at = None
                self._snapshot_error = None
                self.last_used = time.monotonic()
                logger.info("sandbox_session_restored", wf_id=self.wf_id)
                return True
            except Exception as exc:
                self._snapshot_error = type(exc).__name__
                self._transition_lifecycle(SessionLifecycleState.SNAPSHOT_FAILED)
                logger.exception(
                    "sandbox_session_restore_failed",
                    wf_id=self.wf_id,
                    failure=self._snapshot_error,
                )
                raise

    async def run_code(
        self, script: str, inputs: dict, *, timeout_s: float, network: str = "egress"
    ) -> dict:
        """Run one agent Skill ``script`` in this workflow's sandbox.

        Serialized per-session. The provider call is SYNC → ``asyncio.to_thread``.
        Writes the run-dir VFS folders back (best-effort) after the run, then
        returns ``{stdout, stderr, exit_code, error}``.

        Network access uses the same global egress controller as Chat,
        Workflow and MCP. The per-wf overlay is rw-bound at
        ``/opt/agent-overlay`` (``/opt/agent-overlay/py`` on PYTHONPATH) so
        installed packages import.
        """
        self._begin_activity()
        try:
            async with self._lock:
                channel_run_dir = (
                    self.workflow_run_dir if self.expose_run else self.run_dir
                )
                res = await asyncio.to_thread(
                    self.provider.run_code,
                    run_dir=channel_run_dir,
                    script=script,
                    inputs=inputs,
                    run_id=f"agent-{self.wf_id}",
                    timeout=timeout_s,
                    extra_ro_binds=self.base_binds,
                    extra_rw_binds=self._rw_binds,
                    network=network,
                    expose_run=self.expose_run,
                )
                await self.writeback_vfs()
                outputs = res.final_outputs or {}
                return {
                    "stdout": outputs.get("stdout", ""),
                    "stderr": outputs.get("stderr", ""),
                    "exit_code": outputs.get("exit_code"),
                    "error": res.error_dict,
                }
        finally:
            self._end_activity()

    async def run_install(
        self, spec: str, *, manager: str = "pip", timeout_s: float = 180
    ) -> dict:
        """Install a pip (or apt) package into this workflow's persistent overlay.

        Runs a FIXED wrapper script through the unified egress controller. The
        package ``spec`` flows ONLY via the stdin ``inputs``
        JSON — the wrapper reads it and passes it as a LIST argv element, never
        interpolated into a shell or the script source (injection-safe). pip
        installs to ``--target /opt/agent-overlay/py`` (on PYTHONPATH), so later
        scripts/commands can import the package. Returns
        ``{status, stdout, stderr, exit_code}``."""
        res = await self.run_code(
            _INSTALL_WRAPPER,
            {"spec": spec, "manager": manager},
            timeout_s=timeout_s,
            network="egress",
        )
        exit_code = res.get("exit_code")
        return {
            "status": "ok" if exit_code == 0 else "error",
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "exit_code": exit_code,
        }

    async def run_command(self, command: str, *, timeout_s: float = 60) -> dict:
        """Run a shell command through the resident workspace worker.

        The command now uses the same warm worker path as read_file/write_file/grep,
        so all agent-visible filesystem tools share one sandbox view. The command
        still runs inside gVisor with ``shell=True``; the string is transported as
        JSON in a fileop job and is never interpolated into host-side code.
        """
        total_started = time.perf_counter()
        logger.warning(
            "agent_sandbox_run_command_start",
            wf_id=self.wf_id,
            expose_run=self.expose_run,
            timeout_s=timeout_s,
            command_len=len(command or ""),
        )
        op_timeout = max(float(timeout_s), 1.0)
        wait_timeout = max(30.0, op_timeout + 10.0)
        submit_started = time.perf_counter()
        res = await self._submit_fileop(
            {
                "op": "exec",
                "command": command,
                "cwd": "/",
                "timeout": op_timeout,
            },
            timeout=wait_timeout,
        )
        logger.warning(
            "agent_sandbox_run_command_submit_done",
            wf_id=self.wf_id,
            ok=bool(res.get("ok")),
            elapsed_ms=int((time.perf_counter() - submit_started) * 1000),
        )
        writeback_started = time.perf_counter()
        await self.writeback_vfs()
        logger.warning(
            "agent_sandbox_run_command_writeback_done",
            wf_id=self.wf_id,
            elapsed_ms=int((time.perf_counter() - writeback_started) * 1000),
        )
        if not res.get("ok"):
            err = str(res.get("error") or "command failed")
            logger.warning(
                "agent_sandbox_run_command_done",
                wf_id=self.wf_id,
                status="error",
                exit_code=124 if "TimeoutExpired" in err else 1,
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
            )
            return {
                "status": "error",
                "stdout": "",
                "stderr": err,
                "exit_code": 124 if "TimeoutExpired" in err else 1,
            }
        exit_code = res.get("exit_code")
        logger.warning(
            "agent_sandbox_run_command_done",
            wf_id=self.wf_id,
            status="ok" if exit_code == 0 else "error",
            exit_code=exit_code,
            stdout_chars=len(res.get("stdout") or ""),
            stderr_chars=len(res.get("stderr") or ""),
            elapsed_ms=int((time.perf_counter() - total_started) * 1000),
        )
        return {
            "status": "ok" if exit_code == 0 else "error",
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "exit_code": exit_code,
        }

    async def prewarm_fileops(self) -> None:
        """Start the resident file/shell worker pool without running a user op.

        ``get_session`` only materializes the host-side VFS/session object. The
        first ``bash``/fs tool still needs the fileop-capable warm workers. Chat
        route/tool prewarm calls this so the first visible command does not pay
        that second cold-start cost.
        """
        self._begin_activity()
        try:
            await self._get_fileop_pool()
        finally:
            self._end_activity()

    async def submit_sandbox_job(self, job: dict, *, timeout: float = 600.0) -> dict:
        """Submit a generic job to this session's resident sandbox job server.

        This is the common execution path for agent-visible shell/file jobs and
        agent-initiated workflow jobs. The session owns one warm gVisor sandbox;
        the in-sandbox job server owns concurrency.
        """
        self._begin_activity()
        try:
            descriptor = dict(job)
            allow_hosts = descriptor.pop("_allow_hosts", ())
            pool = await self._get_fileop_pool()
            if pool is None:
                raise RuntimeError("no sandbox for this session")
            lease_id = await asyncio.to_thread(
                pool.acquire_egress_hosts, allow_hosts
            )
            try:
                return await asyncio.to_thread(
                    pool.submit_sandbox_job,
                    descriptor,
                    timeout=timeout,
                )
            finally:
                await asyncio.to_thread(pool.release_egress_hosts, lease_id)
        finally:
            self._end_activity()

    async def mcp_manifest(self, server: dict, *, timeout_s: float = 30.0) -> dict:
        """Return a serializable MCP tool manifest from inside this chat sandbox."""
        allow_hosts = await self._mcp_server_egress_hosts(server)
        return await self.submit_sandbox_job(
            {
                "kind": "mcp",
                "_allow_hosts": sorted(allow_hosts),
                "op": {
                    "action": "manifest",
                    "server": server,
                    "timeout_s": timeout_s,
                },
            },
            timeout=max(timeout_s + 10.0, 30.0),
        )

    async def mcp_call(
        self,
        server: dict,
        *,
        tool_name: str,
        arguments: dict,
        timeout_s: float = 120.0,
    ) -> dict:
        """Call one MCP tool from inside this chat sandbox."""
        allow_hosts = await self._mcp_server_egress_hosts(server)
        return await self.submit_sandbox_job(
            {
                "kind": "mcp",
                "_allow_hosts": sorted(allow_hosts),
                "op": {
                    "action": "call",
                    "server": server,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "timeout_s": timeout_s,
                },
            },
            timeout=max(timeout_s + 10.0, 30.0),
        )

    async def _mcp_server_egress_hosts(self, server: dict) -> set[str]:
        """Validate and return one MCP call's scoped public destinations."""
        from vibecanvas_api.services.mcp_config import (
            validate_mcp_connection_destination,
        )

        connection = server.get("connection") if isinstance(server, dict) else None
        if not isinstance(connection, dict):
            return set()  # stdio MCP has no remote destination to authorize
        return set(await validate_mcp_connection_destination(connection))

    async def run_agent_runtime_stream(self, request: dict):
        """Run one Agent Runtime turn on this Chat's warm gVisor process.

        The private UDS carries the request and stream, so credentials are never
        written into the workspace channel. Consecutive turns reuse the process
        and imported Runtime modules; session close/TTL owns final teardown.
        """
        total_started = time.perf_counter()
        self._begin_activity()
        runtime_turn_id = str(request.get("turn_id") or "runtime")
        runtime_type = str(request.get("runtime_type") or "unknown")
        runtime_model = request.get("model")
        uses_codex_account = (
            runtime_type == "codex"
            and isinstance(runtime_model, dict)
            and runtime_model.get("connection_type") == "chatgpt_account"
        )
        broker: BusBroker | None = None
        turn_registered = False
        invalidate_runtime = False
        received_result = False
        try:
            if self.skills_dir and self.user_id:
                from vibecanvas_api.services.runtime_skills import (
                    hydrate_runtime_skills,
                )
                phase_started = time.perf_counter()
                await hydrate_runtime_skills(
                    destination=self.skills_dir,
                    tenant_id=self.tenant_id,
                    skills=request.get("skills") or [],
                )
                logger.info(
                    "agent_runtime_transport_timing",
                    phase="hydrate_skills",
                    elapsed_ms=int((time.perf_counter() - phase_started) * 1000),
                    runtime_type=runtime_type,
                    wf_id=self.wf_id,
                    turn_id=runtime_turn_id,
                )
            async with self._lock:
                invalidate_runtime = True
                broker, _ = await self._ensure_agent_runtime_locked(
                    runtime_type=runtime_type,
                    runtime_turn_id=runtime_turn_id,
                    uses_codex_account=uses_codex_account,
                )
                runtime_request = self._mcp_runtime_request(request)
                # Interactive Runtimes are Chat-scoped and remain resident across
                # Turns.  Codex account sessions follow the same lifecycle as
                # API-backed Codex: explicit account disconnect,
                # Chat/session close, idle hibernation/TTL, or a transport error
                # owns teardown.  ``invalidate_codex_account_sessions`` closes
                # every locally-owned session for the disconnected principal so
                # an idle Runtime cannot retain a revoked account credential.
                invalidate_runtime = False

                async with self._runtime_broker_lock:
                    if runtime_turn_id in self._runtime_brokers:
                        raise RuntimeError(
                            f"agent runtime turn {runtime_turn_id} is already active"
                        )
                    self._runtime_brokers[runtime_turn_id] = broker
                    turn_registered = True
                phase_started = time.perf_counter()
                try:
                    await broker.send(
                        {
                            "type": MSG_RUNTIME_REQUEST,
                            "request": runtime_request,
                        }
                    )
                except (ConnectionError, BrokenPipeError):
                    # The guest can finish closing Turn-local receivers just as
                    # the host observes a terminal boundary.  Never surface a
                    # stale resident transport as a failed user Turn: stop that
                    # process, restore one clean Runtime, and submit exactly
                    # once more before any product event has been accepted.
                    logger.warning(
                        "agent_runtime_stale_transport_recovered",
                        runtime_type=runtime_type,
                        wf_id=self.wf_id,
                        turn_id=runtime_turn_id,
                    )
                    await self._stop_agent_runtime_locked()
                    broker, _ = await self._ensure_agent_runtime_locked(
                        runtime_type=runtime_type,
                        runtime_turn_id=runtime_turn_id,
                        uses_codex_account=uses_codex_account,
                    )
                    runtime_request = self._mcp_runtime_request(request)
                    async with self._runtime_broker_lock:
                        self._runtime_brokers[runtime_turn_id] = broker
                    await broker.send(
                        {
                            "type": MSG_RUNTIME_REQUEST,
                            "request": runtime_request,
                        }
                    )
                first_bus_message = True
                async for message in broker.messages():
                    if first_bus_message:
                        first_bus_message = False
                        logger.info(
                            "agent_runtime_transport_timing",
                            phase="first_sandbox_message",
                            elapsed_ms=int(
                                (time.perf_counter() - phase_started) * 1000
                            ),
                            runtime_type=runtime_type,
                            wf_id=self.wf_id,
                            turn_id=runtime_turn_id,
                            message_type=message.get("type"),
                        )
                    kind = message.get("type")
                    if kind == MSG_RUNTIME_EVENT:
                        event = message.get("event")
                        if isinstance(event, dict):
                            await self._write_through_runtime_tool_event(event)
                            yield event
                    elif kind == MSG_RUNTIME_RESULT:
                        received_result = True
                        break
                    elif kind == MSG_RUNTIME_ERROR:
                        error = message.get("error") or {}
                        raise RuntimeError(
                            str(error.get("message") or "agent runtime failed")
                        )
                if not received_result:
                    invalidate_runtime = True
                    raise RuntimeError(
                        "agent runtime disconnected before completing the turn"
                    )

                # The turn is quiescent here. Persist its workspace mutations
                # without discarding the warm Runtime process.
                try:
                    phase_started = time.perf_counter()
                    await asyncio.shield(self.writeback_vfs())
                    logger.info(
                        "agent_runtime_transport_timing",
                        phase="workspace_writeback",
                        elapsed_ms=int((time.perf_counter() - phase_started) * 1000),
                        runtime_type=runtime_type,
                        wf_id=self.wf_id,
                        turn_id=runtime_turn_id,
                    )
                except Exception:
                    logger.warning(
                        "agent_runtime_writeback_failed",
                        wf_id=self.wf_id,
                        exc_info=True,
                    )
                runtime_volume = getattr(self, "runtime_volume", None)
                if runtime_volume is not None:
                    if uses_codex_account:
                        self._persist_codex_account_auth()
                    phase_started = time.perf_counter()
                    await asyncio.to_thread(
                        get_chat_runtime_volume_provider().sync,
                        runtime_volume,
                    )
                    logger.info(
                        "agent_runtime_transport_timing",
                        phase="runtime_volume_encrypted_sync",
                        elapsed_ms=int(
                            (time.perf_counter() - phase_started) * 1000
                        ),
                        runtime_type=runtime_type,
                        wf_id=self.wf_id,
                        turn_id=runtime_turn_id,
                    )

        except (ConnectionError, BrokenPipeError):
            invalidate_runtime = True
            raise
        finally:
            # A consumer cancellation (the normal host-side Stop path) closes
            # this async generator at its current yield. In that case the
            # resident process may still publish the cancelled Turn's trailing
            # events and runtime_result onto the shared bus. Reusing that bus
            # lets the next Turn consume the stale result and appear to finish
            # immediately with NO_OP. Only a Runtime whose own result boundary
            # was observed is safe to retain across Turns; the sandbox and VFS
            # remain resident while this process-local transport is replaced.
            if turn_registered and not received_result:
                invalidate_runtime = True
            if turn_registered:
                async with self._runtime_broker_lock:
                    if self._runtime_brokers.get(runtime_turn_id) is broker:
                        self._runtime_brokers.pop(runtime_turn_id, None)
            if invalidate_runtime:
                async with self._lock:
                    await self._stop_agent_runtime_locked()
                # A checkpoint can be emitted before a Turn reaches its
                # terminal result (for example before a later egress failure or
                # user cancellation). Persist the stopped Runtime's native
                # files here as well, otherwise the durable checkpoint may
                # reference a rollout that disappears with this plaintext
                # materialization and cannot be resumed after sandboxd restarts.
                runtime_volume = getattr(self, "runtime_volume", None)
                if runtime_volume is not None:
                    try:
                        await asyncio.to_thread(
                            get_chat_runtime_volume_provider().sync,
                            runtime_volume,
                        )
                        logger.info(
                            "agent_runtime_transport_timing",
                            phase="runtime_volume_failure_sync",
                            runtime_type=runtime_type,
                            wf_id=self.wf_id,
                            turn_id=runtime_turn_id,
                        )
                    except Exception:
                        # Preserve the original Runtime error. The exact
                        # missing-rollout recovery in the Codex adapter handles
                        # a previously incomplete snapshot on the next Turn.
                        logger.warning(
                            "agent_runtime_failure_volume_sync_failed",
                            runtime_type=runtime_type,
                            wf_id=self.wf_id,
                            turn_id=runtime_turn_id,
                            exc_info=True,
                        )
            self._end_activity()
            logger.info(
                "agent_runtime_transport_timing",
                phase="transport_total",
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
                runtime_type=runtime_type,
                wf_id=self.wf_id,
                turn_id=runtime_turn_id,
            )

    async def _ensure_agent_runtime_locked(
        self,
        *,
        runtime_type: str,
        runtime_turn_id: str,
        uses_codex_account: bool,
    ) -> tuple[BusBroker, bool]:
        """Return a connected resident Runtime. Caller must hold ``_lock``."""
        if (
            self._bound_runtime_type is not None
            and (
                self._bound_runtime_type != runtime_type
                or self._bound_runtime_uses_codex_account != uses_codex_account
            )
        ):
            raise RuntimeError("sandbox_runtime_binding_mismatch")
        handle = self._runtime_handle
        process_alive = (
            handle is not None
            and getattr(handle, "proc", None) is not None
            and handle.proc.poll() is None
        )
        if (
            process_alive
            and self._runtime_broker is not None
            and self._runtime_broker.is_connected()
            and self._runtime_type == runtime_type
            and self._runtime_uses_codex_account == uses_codex_account
        ):
            logger.info(
                "agent_runtime_transport_timing",
                phase="sandbox_bus_connect",
                elapsed_ms=0,
                runtime_type=runtime_type,
                wf_id=self.wf_id,
                turn_id=runtime_turn_id,
                reused=True,
            )
            return self._runtime_broker, True

        await self._stop_agent_runtime_locked()
        broker = BusBroker(
            socket_path_for(
                f"runtime-session:{self.tenant_id}:{self.wf_id}:{runtime_type}"
            )
        )
        try:
            phase_started = time.perf_counter()
            await broker.start()
            self._runtime_broker = broker
            self._runtime_type = runtime_type
            self._runtime_uses_codex_account = uses_codex_account
            logger.info(
                "agent_runtime_transport_timing",
                phase="broker_start",
                elapsed_ms=int((time.perf_counter() - phase_started) * 1000),
                runtime_type=runtime_type,
                wf_id=self.wf_id,
                turn_id=runtime_turn_id,
            )
            rw_binds, ro_binds, env_overrides = (
                self._agent_runtime_launch_spec(
                    runtime_type=runtime_type,
                    uses_codex_account=uses_codex_account,
                )
            )
            from .agent_runtime_snapshot import get_agent_runtime_baseline

            baseline = (
                get_agent_runtime_baseline(runtime_type)
                if (
                    config.sandbox_resident_mode == "snapshot"
                    and (runtime_type != "codex" or self.runtime_dir is not None)
                )
                else None
            )
            phase_started = time.perf_counter()
            handle = await asyncio.to_thread(
                self.provider.launch_agent_runtime_bus,
                run_id=f"agent-runtime-{self.wf_id}-{runtime_type}",
                bus_socket=broker.socket_path,
                tenant=self.tenant_id,
                extra_rw_binds=rw_binds,
                extra_ro_binds=ro_binds,
                env_overrides=env_overrides,
                snapshot=baseline,
            )
            self._runtime_handle = handle
            logger.info(
                "agent_runtime_transport_timing",
                phase="sandbox_process_launch",
                elapsed_ms=int((time.perf_counter() - phase_started) * 1000),
                runtime_type=runtime_type,
                wf_id=self.wf_id,
                turn_id=runtime_turn_id,
                restored_from_baseline=baseline is not None,
            )
            phase_started = time.perf_counter()
            await self._wait_for_agent_runtime_connection(
                broker,
                handle,
                timeout=30.0,
                restored_from_baseline=baseline is not None,
            )
            self._runtime_process_generation += 1
            self._bound_runtime_type = runtime_type
            self._bound_runtime_uses_codex_account = uses_codex_account
            logger.info(
                "agent_runtime_transport_timing",
                phase="sandbox_bus_connect",
                elapsed_ms=int((time.perf_counter() - phase_started) * 1000),
                runtime_type=runtime_type,
                wf_id=self.wf_id,
                turn_id=runtime_turn_id,
                reused=False,
            )
            return broker, False
        except Exception as exc:
            raw_failure = str(exc).strip()
            failure_code = (
                raw_failure
                if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", raw_failure)
                else type(exc).__name__
            )
            logger.exception(
                "agent_runtime_start_failed",
                runtime_type=runtime_type,
                wf_id=self.wf_id,
                turn_id=runtime_turn_id,
                uses_codex_account=uses_codex_account,
                failure_code=failure_code,
                failure_errno=getattr(exc, "errno", None),
            )
            await self._stop_agent_runtime_locked()
            raise

    def _mcp_runtime_request(self, request: dict) -> dict:
        """Attach secret-free Hub contracts after the sandbox epoch is known.

        Host authority descriptors are consumed here and never serialized into
        the sandbox. The Runtime receives only sandbox-owned lifecycle contracts.
        """
        from vibecanvas_api.services.agent_runtime.mcp_desired_state import (
            build_mcp_lifecycle_contracts,
        )
        from vibecanvas_api.services.agent_runtime.mcp_execution_capability import (
            mint_mcp_execution_capability,
        )
        from vibecanvas_api.services.agent_runtime.model_capability import (
            authorization_model_generation,
        )
        from vibecanvas_api.services.agent_runtime.protocol import (
            RuntimeTurnRequest,
        )

        parsed = RuntimeTurnRequest.model_validate(request)
        authorization_generation = authorization_model_generation(
            model_id=config.openfga_authorization_model_id,
        )
        execution_capability = mint_mcp_execution_capability(
            organization_id=parsed.tenant_id,
            user_id=parsed.user_id,
            chat_id=parsed.chat_id,
            runtime_session_id=parsed.runtime_session_id,
            turn_id=parsed.turn_id,
            sandbox_id=self._sandbox_runtime_id,
            sandbox_generation=self._runtime_process_generation,
            selected_mcp_revision=parsed.mcp_config_revision,
            active_platform_capabilities=list(parsed.active_platform_mcps),
            authorization_generation=authorization_generation,
            secret=config.signing_secret,
            ttl_s=config.mcp.platform_capability_ttl_s,
        )
        desired, execution = build_mcp_lifecycle_contracts(
            parsed,
            sandbox_id=self._sandbox_runtime_id,
            sandbox_generation=self._runtime_process_generation,
            authorization_generation=authorization_generation,
            execution_capability=execution_capability,
            lifetime_s=config.mcp.platform_capability_ttl_s,
        )
        projected = parsed.model_copy(update={
            "mcp_runtime_stage": "sandbox",
            "mcp_desired_state": desired,
            "mcp_execution_context": execution,
            "mcp_host_servers": [],
        })
        payload = projected.model_dump(mode="json")
        # SecretStr intentionally masks generic serialization. This capability
        # is nevertheless the private Runtime bus credential by design, so put
        # it only into its typed wire field after every Host authority has been
        # removed. It never enters logs, VFS, model context, or browser events.
        payload["mcp_execution_context"]["capability"] = execution_capability
        return payload

    @staticmethod
    async def _wait_for_agent_runtime_connection(
        broker: BusBroker,
        handle: object,
        *,
        timeout: float,
        restored_from_baseline: bool,
    ) -> None:
        """Race the Runtime bus handshake against an early runsc exit.

        ``runsc restore`` is asynchronous after its successful ``create``. A
        mount-contract or image error can therefore terminate it immediately.
        Waiting only for the bus hid that useful failure behind a 30-second
        timeout and surfaced to the browser as a generic disconnect.
        """

        connection = asyncio.create_task(broker.wait_connected())
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("agent_runtime_bus_connect_timeout")
                done, _pending = await asyncio.wait(
                    {connection},
                    timeout=min(0.05, remaining),
                )
                if connection in done:
                    await connection
                    return
                process = getattr(handle, "proc", None)
                if process is not None and process.poll() is not None:
                    code = (
                        "agent_runtime_baseline_restore_exited"
                        if restored_from_baseline
                        else "agent_runtime_process_exited_before_connect"
                    )
                    raise RuntimeError(code)
        finally:
            if not connection.done():
                connection.cancel()
                try:
                    await connection
                except asyncio.CancelledError:
                    pass

    def _agent_runtime_launch_spec(
        self,
        *,
        runtime_type: str,
        uses_codex_account: bool,
    ) -> tuple[
        list[tuple[str, str]],
        list[str | tuple[str, str]],
        dict[str, str],
    ]:
        """Build the mount/env contract shared by baseline and live restore."""

        rw_binds = list(self._rw_binds)
        ro_binds: list[str | tuple[str, str]] = list(self.base_binds)
        if self.skills_dir:
            ro_binds.append(("/skills", self.skills_dir))
        env_overrides: dict[str, str] = {
            "AGENT_DEBUG_VIEW_ENABLED": (
                "1" if config.agent_debug_view_enabled else "0"
            ),
            "VC_AGENT_RUNTIME_TYPE": runtime_type,
        }
        if runtime_type != "codex":
            return rw_binds, ro_binds, env_overrides

        executable = resolve_codex_executable()
        if executable is None:
            raise RuntimeError("codex_cli_unavailable")
        package_root = codex_cli_readonly_root(executable)
        if package_root not in ro_binds:
            ro_binds.append(package_root)
        # Built-in MCP launchers and their official rendering dependency are
        # symlinks under /usr while their immutable packages live under /opt.
        # The generic system mounts expose each symlink but not its target.
        # Bind every resolved package root read-only so the command is
        # executable inside gVisor; none contains credentials or platform
        # state.
        for env_name, default_command in (
            ("PLAYWRIGHT_MCP_COMMAND", "flowork-playwright-mcp"),
            ("DIAGRAM_MCP_COMMAND", "flowork-diagram-mcp"),
            ("DRAWIO_CLI_COMMAND", "drawio"),
        ):
            command = str(
                os.environ.get(env_name) or default_command
            ).strip()
            executable_path = (
                command if os.path.isabs(command) else shutil.which(command)
            )
            if not executable_path:
                continue
            resolved_executable = os.path.realpath(executable_path)
            if not os.path.isfile(resolved_executable):
                continue
            package_root = os.path.dirname(resolved_executable)
            if package_root not in ro_binds:
                ro_binds.append(package_root)
        node_runtime = codex_cli_node_runtime(executable)
        if executable.endswith(".js") and node_runtime is None:
            raise RuntimeError("codex_node_runtime_unavailable")
        if node_runtime is not None:
            node_root = os.path.dirname(node_runtime)
            if node_root not in ro_binds:
                ro_binds.append(node_root)
            env_overrides["PATH"] = os.pathsep.join(
                (node_root, os.environ.get("PATH", ""))
            )
        env_overrides["CODEX_CLI_PATH"] = executable
        for key in (
            "CODEX_APP_SERVER_JSONL_LIMIT_BYTES",
            "CODEX_PLATFORM_MCP_TOOL_TIMEOUT_S",
        ):
            value = os.environ.get(key)
            if value:
                env_overrides[key] = value

        # ``/runtime`` is already a private Chat-owned writable mount. Stage
        # account auth into that directory instead of mounting auth.json over
        # it: Codex refreshes credentials with an atomic rename, and replacing
        # a bind-mount point fails with EBUSY. The staged file is excluded from
        # Runtime Volume persistence and is reconciled to the user-scoped cache
        # at the quiescent Turn boundary.
        self._stage_codex_runtime_auth(uses_codex_account=uses_codex_account)
        return rw_binds, ro_binds, env_overrides

    @staticmethod
    def _read_regular_private_file(path: str, *, limit: int = 4 * 1024 * 1024) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise RuntimeError("codex_account_auth_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                return handle.read(limit + 1)
        finally:
            os.close(descriptor)

    @staticmethod
    def _replace_private_file(path: str, data: bytes, *, owner: tuple[int, int] | None = None) -> None:
        parent = os.path.dirname(path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".auth-", dir=parent)
        try:
            os.fchmod(descriptor, 0o600)
            if owner is not None:
                os.fchown(descriptor, owner[0], owner[1])
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _stage_codex_runtime_auth(self, *, uses_codex_account: bool) -> None:
        if not self.runtime_dir:
            if uses_codex_account:
                raise RuntimeError("codex_runtime_volume_unavailable")
            return
        target = os.path.join(self.runtime_dir, ".codex", "auth.json")
        if uses_codex_account:
            source = self.account_auth_file
            if not source or not os.path.isfile(source) or os.path.islink(source):
                raise RuntimeError("codex_account_not_connected")
            data = self._read_regular_private_file(source)
            try:
                parsed = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("codex_account_auth_invalid") from exc
            if not isinstance(parsed, dict):
                raise RuntimeError("codex_account_auth_invalid")
        else:
            data = b""
        self._replace_private_file(target, data)

    def _persist_codex_account_auth(self) -> None:
        if not self.runtime_dir:
            return
        source = os.path.join(self.runtime_dir, ".codex", "auth.json")
        target = self.account_auth_file
        if (
            not target
            or not os.path.isfile(target)
            or os.path.islink(target)
            or not os.path.isfile(source)
            or os.path.islink(source)
        ):
            return
        data = self._read_regular_private_file(source)
        try:
            parsed = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("codex_runtime_auth_refresh_invalid", wf_id=self.wf_id)
            return
        if not isinstance(parsed, dict):
            logger.warning("codex_runtime_auth_refresh_invalid", wf_id=self.wf_id)
            return
        metadata = os.stat(target, follow_symlinks=False)
        self._replace_private_file(
            target,
            data,
            owner=(metadata.st_uid, metadata.st_gid),
        )

    async def _stop_agent_runtime_locked(self) -> None:
        """Stop the warm main Runtime. Caller must hold ``self._lock``."""
        handle = getattr(self, "_runtime_handle", None)
        broker = getattr(self, "_runtime_broker", None)
        persist_account_auth = self._runtime_uses_codex_account
        self._runtime_handle = None
        self._runtime_broker = None
        self._runtime_type = None
        self._runtime_uses_codex_account = False
        if broker is not None:
            await broker.close()
        if handle is not None:
            # Closing the private bus lets the resident Runtime unwind its
            # app-server and MCP resources before runsc teardown.  The provider
            # still escalates to a forced container kill if it does not exit
            # within the bounded grace period.
            await asyncio.to_thread(self.provider.stop_run, handle, kill=False)
        if persist_account_auth:
            self._persist_codex_account_auth()

    async def send_agent_runtime_control(self, turn_id: str, response: dict) -> None:
        """Send a durable platform decision to the active sandbox Runtime.

        The caller supplies a validated RuntimeControlResponse. Keeping this
        method payload-agnostic prevents SDK types from leaking into the
        SandboxManager; the in-sandbox adapter performs the final translation.
        """
        async with self._runtime_broker_lock:
            broker = self._runtime_brokers.get(turn_id)
        if broker is None:
            raise LookupError(f"agent runtime turn {turn_id} is not active")
        await broker.send({"type": MSG_RUNTIME_CONTROL, "response": response})

    async def cancel_agent_runtime(self, turn_id: str) -> bool:
        """Request cancellation without requiring a HITL correlation object."""
        async with self._runtime_broker_lock:
            broker = self._runtime_brokers.get(turn_id)
        if broker is None:
            return False
        await broker.send({
            "type": MSG_RUNTIME_CONTROL,
            "response": {"action": "cancel", "turn_id": turn_id},
        })
        return True

    async def submit_workflow_stream(
        self,
        *,
        workflow: dict,
        inputs: dict,
        run_id: str,
        tenant: str,
        run_subpath: str | None = None,
        run_dir: str | None = None,
        extra: dict | None = None,
        code_pythonpath: str | None = None,
        allow_hosts: set[str] | list[str] | tuple[str, ...] = (),
        timeout: float = 120.0,
    ):
        """Stream one workflow job through this session's resident sandbox.

        ``run_id`` is the engine/run-context id seen inside the sandbox. For the
        interactive workflow surface this is the fixed workflow id.
        ``run_subpath`` points at the fixed object-store run directory under
        the pool's ``/runs`` mount.
        """
        self._begin_activity()
        try:
            if tenant != self.tenant_id:
                raise ValueError("workflow tenant does not match sandbox scope")
            async with self._workflow_job_lock:
                pool = await self._get_fileop_pool()
                if pool is None:
                    raise RuntimeError("no sandbox for this session")
                sub = (run_subpath or self.workflow_run_id or run_id).strip("/")
                if (
                    not sub or "\\" in sub or "\x00" in sub
                    or any(part in {"", ".", ".."} for part in sub.split("/"))
                ):
                    raise ValueError("invalid workflow run subpath")
                # Never trust a host path supplied over RPC. The daemon-owned
                # descriptor is the sole mount/staging authority.
                del run_dir
                host_run_dir = self.workflow_run_dir
                if not host_run_dir:
                    raise RuntimeError("workflow sandbox has no /run directory")
                from vibecanvas_api.services.workflow_sandbox_runner import (
                    stage_workflow_job,
                )
                staged_extra = dict(extra or {})
                if code_pythonpath:
                    staged_extra["code_pythonpath"] = code_pythonpath
                await asyncio.to_thread(
                    stage_workflow_job,
                    os.path.dirname(host_run_dir),
                    sub,
                    workflow,
                    inputs,
                    staged_extra or None,
                )
                lease_id = await asyncio.to_thread(
                    pool.acquire_egress_hosts, allow_hosts
                )
                try:
                    async for msg in pool.submit_stream(
                        workflow=workflow,
                        inputs=inputs,
                        run_id=run_id,
                        tenant=tenant,
                        run_subpath=sub,
                        timeout=timeout,
                    ):
                        yield msg
                finally:
                    await asyncio.to_thread(
                        pool.release_egress_hosts, lease_id
                    )
        finally:
            self._end_activity()

    async def clear_workflow_run(self, workflow_run_id: str | None = None) -> None:
        """Clear one stable Workflow ``/run`` projection in sandboxd.

        A Chat workspace id and its selected Workflow id are independent.  The
        caller therefore supplies the logical Workflow run id instead of
        implicitly clearing the Chat workspace directory.
        """
        target = (workflow_run_id or self.workflow_run_id or "").strip("/")
        if not target:
            return
        if (
            "\\" in target
            or "\x00" in target
            or any(part in {"", ".", ".."} for part in target.split("/"))
        ):
            raise ValueError("invalid workflow run id")
        self._begin_activity()
        try:
            from vibecanvas_api.services.vfs_run_context import clear_run_contents
            await clear_run_contents(target, self.tenant_id)
        finally:
            self._end_activity()

    async def submit_node_job(
        self,
        *,
        node: dict,
        inputs: dict,
        extra: dict | None,
        tenant: str,
        run_id: str,
        run_subpath: str,
        timeout: float,
    ) -> dict:
        """Stage, execute and read one node without leaking a host path."""
        self._begin_activity()
        try:
            if not self.workflow_run_dir:
                raise RuntimeError("workflow sandbox has no /run directory")
            if tenant != self.tenant_id:
                raise ValueError("workflow tenant does not match sandbox scope")
            normalized_subpath = run_subpath.strip("/")
            if (
                not normalized_subpath or "\\" in normalized_subpath
                or "\x00" in normalized_subpath
                or any(part in {"", ".", ".."} for part in normalized_subpath.split("/"))
            ):
                raise ValueError("invalid workflow run subpath")
            from vibecanvas_api.services.sandbox.egress_policy import (
                compute_allow_hosts,
            )
            credential_map = (extra or {}).get("llm_credentials") or {}
            node_allow_hosts = compute_allow_hosts(
                {str(node.get("node_id") or "node"): node},
                user_id=self.user_id or "",
                creds_mapping=credential_map,
            )
            from vibecanvas_api.services.workflow_sandbox_runner import (
                read_result_json,
                stage_node_job,
            )
            runs_root = os.path.dirname(self.workflow_run_dir)
            target_run_dir = os.path.join(runs_root, normalized_subpath)
            async with self._workflow_job_lock:
                await asyncio.to_thread(
                    stage_node_job, target_run_dir, node, inputs, extra,
                )
                status = await self.submit_sandbox_job(
                    {
                        "kind": "node", "tenant": tenant, "run_id": run_id,
                        "run_subpath": normalized_subpath,
                        "_allow_hosts": sorted(node_allow_hosts),
                    },
                    timeout=timeout,
                )
                result = read_result_json(runs_root, normalized_subpath)
            await self.writeback_vfs()
            return {"status": status, "result": result}
        finally:
            self._end_activity()

    async def execute_workflow_job(
        self,
        *,
        workflow: dict,
        inputs: dict,
        extra: dict | None,
        tenant: str,
        run_id: str,
        run_subpath: str,
        timeout: float = 600.0,
    ) -> dict:
        """Stage, execute and collect one workflow entirely inside sandboxd.

        The caller supplies logical data only.  In particular there is no host
        path argument: object-store materialization and the writable mount are
        owned by this process.  Distinct subpaths may run concurrently through
        the resident pool, which is the primitive used by batch execution.
        """
        self._begin_activity()
        try:
            if tenant != self.tenant_id:
                raise ValueError("workflow tenant does not match sandbox scope")
            normalized_subpath = run_subpath.strip("/")
            if (
                not normalized_subpath or "\\" in normalized_subpath
                or "\x00" in normalized_subpath
                or any(
                    part in {"", ".", ".."}
                    for part in normalized_subpath.split("/")
                )
            ):
                raise ValueError("invalid workflow run subpath")
            if not self.workflow_run_dir:
                raise RuntimeError("workflow sandbox has no /run directory")

            from vibecanvas_api.services.sandbox.egress_policy import (
                compute_allow_hosts,
            )
            credential_map = (extra or {}).get("llm_credentials") or {}
            workflow_allow_hosts = compute_allow_hosts(
                workflow,
                user_id=self.user_id or "",
                creds_mapping=credential_map,
            )

            from vibecanvas_api.services.workflow_sandbox_runner import (
                read_result_json,
                stage_workflow_job,
            )

            await asyncio.to_thread(
                stage_workflow_job,
                os.path.dirname(self.workflow_run_dir),
                normalized_subpath,
                workflow,
                inputs,
                extra,
            )
            status = await self.submit_sandbox_job(
                {
                    "kind": "workflow",
                    "tenant": tenant,
                    "run_id": run_id,
                    "run_subpath": normalized_subpath,
                    "_allow_hosts": sorted(workflow_allow_hosts),
                },
                timeout=timeout,
            )
            result = read_result_json(
                os.path.dirname(self.workflow_run_dir), normalized_subpath,
            )
            return {"status": status, "result": result}
        finally:
            self._end_activity()

    async def cancel_workflow_run(
        self,
        *,
        run_id: str,
        tenant: str,
        run_subpath: str | None = None,
    ) -> None:
        """Hard-cancel a workflow job running in this resident sandbox."""
        self._begin_activity()
        try:
            pool = await self._get_fileop_pool()
            if pool is None:
                return
            sub = (run_subpath or self.workflow_run_id or run_id).strip("/")
            await asyncio.to_thread(
                pool.cancel, run_id=run_id, tenant=tenant, run_subpath=sub)
        finally:
            self._end_activity()

    # -- warm-backed file API (Task 4b-ii) ---------------------------------
    async def _get_fileop_pool(self) -> WarmGvisorPool | None:
        """Lazily build (ONCE) + return this session's SECURE warm file-op pool,
        or ``None`` when no sandbox is possible (``run_dir is None`` — InMemory /
        no wf).

        The worker mounts the agent's CLEAN file roots — the ``/data /memory
        /logs`` writeback folders (filtered out of ``self._rw_binds`` so the
        ``/opt/agent-overlay`` overlay is NOT a fileop root). Workflow-associated
        sessions may also expose ``/run`` for run-tier results; pure Chat
        sessions do not expose it, so implementation files like ``__exec__`` and
        duplicate ``/run/data`` paths stay hidden from the agent.

        ``fileops=True`` → no DB or secret env; network follows
        ``config.sandbox_network``. ``store_root``/``work_root`` are
        session-scoped staging dirs in a SIBLING of ``run_dir`` (``run_dir +
        ".fileops"``), NOT under it: workflow-associated sessions may expose a
        run tier, and staging under a mounted tree would leak the channel
        inbox/outbox to the agent. The sibling stays off all user-visible roots.

        Built under ``_fileop_lock`` (double-checked) so two concurrent first
        file ops boot exactly one gVisor sandbox. ``config.sandbox_fileop_workers``
        is the in-sandbox job concurrency, not the number of gVisor instances.
        ``.start()`` is blocking (cold boot) → offloaded via ``asyncio.to_thread``."""
        if self.run_dir is None:
            return None
        if self._fileop_pool is not None:
            return self._fileop_pool
        async with self._fileop_lock:
            if self._fileop_pool is not None:  # double-checked
                return self._fileop_pool
            started = time.perf_counter()
            logger.warning(
                "agent_sandbox_fileop_pool_start",
                wf_id=self.wf_id,
                workers=max(1, int(config.sandbox_fileop_workers)),
                expose_run=self.expose_run,
            )
            # The agent's file mounts: the writeback folders from ``_rw_binds``
            # (excluding /opt/agent-overlay from the roots) plus /mount.
            writeback_dests = {f"/{f}" for f in _RUN_WRITEBACK_FOLDERS}
            if self.mount_dir:
                writeback_dests.add("/mount")
            fileop_binds: list[tuple[str, str]] = [
                (dest, src) for dest, src in self._rw_binds
                if dest in writeback_dests
            ]
            fileop_roots = [dest for dest, _src in fileop_binds]
            if self.expose_run and self.workflow_run_dir:
                # Workflow-associated sessions expose the workflow run tier for
                # run/debug-result inspection. Pure Chat sessions intentionally
                # omit it so the agent only sees clean workspace roots.
                fileop_binds.append(("/run", self.workflow_run_dir))
                fileop_roots.append("/run")
            # Session-scoped staging in a SIBLING of run_dir (NOT under it): never
            # place fileop inbox/outbox under a user-visible mount.
            staging = self.run_dir.rstrip("/") + ".fileops"
            store = get_object_store()
            # Warm gVisor binds only the daemon-private plaintext projection.
            # The durable Object Store root and S3/KMS credentials are never
            # present inside the sandbox.
            store_root = (
                store.materialized_root
                if isinstance(store, FilesystemObjectStore)
                else os.path.dirname(self.pool_runs_root or self.run_dir)
            )
            work_root = os.path.join(staging, "work")
            os.makedirs(work_root, exist_ok=True)
            pool = WarmGvisorPool(
                provider=self.provider,
                store_root=store_root,
                work_root=work_root,
                size=max(1, int(config.sandbox_fileop_workers)),
                tenant=self.tenant_id,
                fileops=True,
                fileop_binds=fileop_binds,
                fileop_roots=fileop_roots,
                materialized_runs_root=self.pool_runs_root,
            )
            await asyncio.to_thread(pool.start)
            self._fileop_pool = pool
            logger.warning(
                "agent_sandbox_fileop_pool_done",
                wf_id=self.wf_id,
                workers=max(1, int(config.sandbox_fileop_workers)),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            return pool

    def _begin_activity(self) -> None:
        state = _session_lifecycle_state(self)
        if state != "warm":
            raise RuntimeError(
                f"sandbox session is {state}; reacquire it before starting work"
            )
        self._inflight_operations += 1
        self.last_used = time.monotonic()

    def _end_activity(self) -> None:
        now = time.monotonic()
        self.last_used = now
        if self._inflight_operations <= 0:
            logger.error(
                "sandbox_activity_lease_underflow",
                wf_id=self.wf_id,
            )
            self._inflight_operations = 0
            return
        self._inflight_operations -= 1
        if self._inflight_operations == 0:
            # Converge the observer immediately instead of waiting for the next
            # daemon poll. A host-authoritative abandoned job or guest activity
            # keeps `busy` true; otherwise the silence clock starts now.
            self.observe_activity(now=now)

    async def _submit_fileop(self, op: dict, *, timeout: float = 30.0) -> dict:
        """Run one file operation under the shared sliding-idle lease."""
        self._begin_activity()
        try:
            return await self._submit_fileop_inner(op, timeout=timeout)
        finally:
            self._end_activity()

    async def _submit_fileop_inner(self, op: dict, *, timeout: float = 30.0) -> dict:
        """Acquire the warm pool + submit ONE op OFF-THREAD; raise a clear error
        when no sandbox is possible."""
        total_started = time.perf_counter()
        logger.warning(
            "agent_sandbox_fileop_submit_start",
            wf_id=self.wf_id,
            op=op.get("op"),
            timeout_s=timeout,
        )
        pool_started = time.perf_counter()
        pool = await self._get_fileop_pool()
        logger.warning(
            "agent_sandbox_fileop_pool_ready",
            wf_id=self.wf_id,
            op=op.get("op"),
            has_pool=pool is not None,
            elapsed_ms=int((time.perf_counter() - pool_started) * 1000),
        )
        if pool is None:
            raise RuntimeError(
                "no sandbox for this session (run_dir is None — InMemory / no "
                "workflow); the warm file API is unavailable")
        submit_started = time.perf_counter()
        res = await asyncio.to_thread(pool.submit_fileop, op, timeout=timeout)
        if not _fileop_should_resubmit(res):
            logger.warning(
                "agent_sandbox_fileop_submit_done",
                wf_id=self.wf_id,
                op=op.get("op"),
                ok=bool(res.get("ok")),
                retried=False,
                exec_elapsed_ms=res.get("exec_elapsed_ms"),
                submit_elapsed_ms=int((time.perf_counter() - submit_started) * 1000),
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
            )
            return res
        logger.warning(
            "agent_fileop_submit_resubmit",
            wf_id=self.wf_id,
            op=op.get("op"),
            error=res.get("error"),
            elapsed_ms=int((time.perf_counter() - submit_started) * 1000),
        )
        retry_started = time.perf_counter()
        retry = await asyncio.to_thread(pool.submit_fileop, op, timeout=timeout)
        logger.warning(
            "agent_sandbox_fileop_submit_done",
            wf_id=self.wf_id,
            op=op.get("op"),
            ok=bool(retry.get("ok")),
            retried=True,
            exec_elapsed_ms=retry.get("exec_elapsed_ms"),
            submit_elapsed_ms=int((time.perf_counter() - retry_started) * 1000),
            elapsed_ms=int((time.perf_counter() - total_started) * 1000),
        )
        return retry

    async def read_file(self, path: str) -> dict:
        """Read ``path`` inside the sandbox. Returns the raw fileop result dict
        (``{"ok":True,"kind":"text","content":...}`` /  ``{"kind":"binary",...}``
        / ``{"ok":False,"error":...}``)."""
        return await self._submit_fileop({"op": "read", "path": path})

    async def write_file(self, path: str, content: str) -> dict:
        """Write ``content`` to ``path`` inside the sandbox. Returns the raw
        fileop result dict (``{"ok":True,"bytes":n}`` / error)."""
        res = await self._submit_fileop(
            {"op": "write", "path": path, "content": content})
        if res.get("ok"):
            await self.writeback_vfs()
        return res

    async def read_bytes(self, path: str) -> dict:
        """Read RAW bytes from ``path`` inside the sandbox (any file — the binary path
        for xlsx etc.). base64 is the transport detail: the returned dict carries the
        decoded bytes in ``data`` (``{"ok":True,"data":<bytes>}`` / error)."""
        res = await self._submit_fileop({"op": "read_bytes", "path": path})
        if res.get("ok") and "data_b64" in res:
            res = {"ok": True, "data": base64.b64decode(res["data_b64"])}
        return res

    async def write_bytes(self, path: str, data: bytes) -> dict:
        """Write RAW bytes to ``path`` inside the sandbox (the binary path for xlsx
        etc.). ``data`` is base64-encoded for transport here. Returns the raw fileop
        result dict (``{"ok":True,"bytes":n}`` / error)."""
        res = await self._submit_fileop(
            {"op": "write_bytes", "path": path,
             "data_b64": base64.b64encode(data).decode("ascii")})
        if res.get("ok"):
            await self.writeback_vfs()
        return res

    async def list_dir(self, path: str) -> dict:
        """List ``path`` inside the sandbox. Returns the raw fileop result dict
        (``{"ok":True,"entries":[...]}`` / error)."""
        return await self._submit_fileop({"op": "list", "path": path})

    async def grep(self, pattern: str, path: str, glob: str = "",
                   context: int = 0) -> dict:
        """Grep ``pattern`` under ``path`` inside the sandbox — optional filename
        ``glob`` + ``context`` lines per match. Returns the raw fileop result dict
        (``{"ok":True,"matches":[...],"match_count":n}`` / error)."""
        return await self._submit_fileop(
            {"op": "grep", "pattern": pattern, "path": path,
             "glob": glob, "context": context})

    async def edit_file(
        self, path: str, old: str, new: str, replace_all: bool = False
    ) -> dict:
        """Exact-string replace ``old`` → ``new`` in ``path`` inside the sandbox.

        Reads the file (fileop ``read``), does the replacement in Python
        (unique-by-default: if ``old`` occurs != 1 time and not ``replace_all``,
        returns ``{"ok":False,"error":"not_unique"|"not_found"}`` WITHOUT
        writing), then writes the result (fileop ``write``). Returns
        ``{"ok":True,"replacements":n,"diff":<git-style unified diff>}`` on success,
        else the propagated read/write error dict."""
        read_res = await self.read_file(path)
        if not read_res.get("ok"):
            return read_res
        if read_res.get("kind") != "text":
            return {"ok": False, "error": "not_text"}
        content = read_res.get("content", "")
        count = content.count(old)
        if count == 0:
            return {"ok": False, "error": "not_found"}
        if count != 1 and not replace_all:
            return {"ok": False, "error": "not_unique"}
        if replace_all:
            new_content = content.replace(old, new)
        else:
            new_content = content.replace(old, new, 1)
            count = 1
        write_res = await self.write_file(path, new_content)
        if not write_res.get("ok"):
            return write_res
        return {"ok": True, "replacements": count,
                "diff": _edit_unified_diff(content, new_content, path),
                "content": new_content}

    async def writeback_vfs(self) -> None:
        """Best-effort write-back of the run's VFS folders to the durable VFS.

        ``/data`` ``/memory`` ``/logs`` live under ``run_dir`` and are mirrored
        with the SAME diff-and-upsert shape via :meth:`_sync_run_folder`. Never
        raises — a write-back failure must never break the agent turn."""
        for folder in _RUN_WRITEBACK_FOLDERS:
            try:
                await self._sync_run_folder(folder)
            except Exception:  # pragma: no cover - fail-soft
                logger.warning("agent_run_writeback_failed", wf_id=self.wf_id,
                               folder=folder, exc_info=True)
        try:
            await self._sync_mount_folder()
        except Exception:  # pragma: no cover - fail-soft
            logger.warning(
                "agent_mount_writeback_failed",
                wf_id=self.wf_id,
                mount_scope_id=self.mount_scope_id,
                exc_info=True,
            )
        try:
            if self.expose_run and self.workflow_run_dir and self.workflow_run_id:
                await sync_run_back(
                    self.workflow_run_id,
                    self.tenant_id,
                    self.workflow_run_dir,
                    self.wf_id,
                )
        except Exception:  # pragma: no cover - fail-soft
            logger.warning("workflow_run_writeback_failed",
                           wf_id=self.wf_id,
                           run_id=self.workflow_run_id,
                           tenant_id=self.tenant_id, exc_info=True)

    async def sync_workspace_path(self, path: str) -> bool:
        """Write one completed sandbox file mutation through to durable VFS.

        A successful Agent file tool must mean that Preview and a later worker
        can read the same bytes immediately. Turn-end folder writeback remains
        only a safety sweep for opaque process writes.
        """
        if self.closed or not self.run_dir:
            return False
        normalized = "/" + str(path or "").strip("/")
        folder = next(
            (
                candidate
                for candidate in _RUN_WRITEBACK_FOLDERS
                if normalized.startswith(f"/{candidate}/")
            ),
            None,
        )
        if folder is None or ".." in normalized.split("/"):
            return False
        relative = normalized[len(folder) + 2 :]
        source_root = os.path.realpath(os.path.join(self.run_dir, folder))
        source = os.path.realpath(os.path.join(source_root, *relative.split("/")))
        if not source.startswith(source_root + os.sep) or not os.path.isfile(source):
            return False

        def _read() -> bytes:
            with open(source, "rb") as handle:
                return handle.read()

        try:
            data = await asyncio.to_thread(_read)
        except OSError:
            logger.warning(
                "agent_workspace_write_through_read_failed",
                wf_id=self.wf_id,
                path=normalized,
                exc_info=True,
            )
            return False
        try:
            async with short_session_scope(tenant_id=self.tenant_id) as session:
                async with self._external_vfs_lock:
                    if normalized in self._external_vfs_fenced_paths:
                        return False
                    await VfsRepo(
                        session,
                        object_store=get_object_store(),
                    ).upsert_artifact_bytes(
                        wf_id=self.wf_id,
                        tenant=self.tenant_id,
                        path=normalized,
                        data=data,
                        content_type=_guess_ct(relative, data),
                    )
            self.last_used = time.monotonic()
            logger.info(
                "agent_workspace_write_through_done",
                wf_id=self.wf_id,
                path=normalized,
                bytes=len(data),
            )
            return True
        except Exception:
            logger.warning(
                "agent_workspace_write_through_failed",
                wf_id=self.wf_id,
                path=normalized,
                exc_info=True,
            )
            return False

    async def _write_through_runtime_tool_event(self, event: dict) -> None:
        """Persist mutations before a successful tool_end reaches the UI."""
        if event.get("type") != "projection":
            return
        projection = event.get("payload")
        if not isinstance(projection, dict) or projection.get("event_type") != "CHAT_EVENT":
            return
        payload = projection.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "tool_end"
            or payload.get("status") != "done"
        ):
            return
        invocation = payload.get("invocation")
        if not isinstance(invocation, dict):
            return
        tool_name = str(invocation.get("name") or "")
        tool_input = invocation.get("input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if tool_name in {"write_file", "edit_file"}:
            await self.sync_workspace_path(str(tool_input.get("path") or ""))
        elif tool_name in {
            "bash",
            "exec_command",
            "apply_patch",
            # Codex app-server projects native commandExecution items as
            # ``shell`` and native patch items as ``file_change``.  Both can
            # mutate several workspace paths, so they need the same completed-
            # tool sweep as the legacy command adapters.  The sweep finishes
            # before tool_end is yielded to the browser, making the VFS view
            # immediately authoritative while the Turn is still running.
            "shell",
            "file_change",
        }:
            # These tools may mutate several paths. The command process has
            # completed at tool_end, so the bounded workspace sweep is stable.
            await self.writeback_vfs()

    async def mirror_vfs_write(self, path: str, data: bytes) -> bool:
        """Mirror one durable VFS write into this already-live sandbox session.

        Browser uploads and file-view saves update the durable VFS first. If a
        resident sandbox is already running, its bind-mounted host dirs would
        otherwise stay stale until the next session rebuild. This method keeps
        the live mount in sync without creating a sandbox.
        """
        resolved = self._external_vfs_target(path)
        if self.closed or resolved is None:
            return False
        _norm, target = resolved

        def _write() -> None:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)

        await asyncio.to_thread(_write)
        now = time.monotonic()
        self.last_used = now
        if _session_lifecycle_state(self) == "hibernated":
            # Browser/VFS activity retains the hibernated session without
            # waking its gVisor process.
            self._hibernated_at = now
        return True

    def _external_vfs_target(self, path: str) -> tuple[str, str] | None:
        """Resolve one VFS path into this session's mounted host workspace."""
        norm = "/" + str(path or "").strip("/")
        if "\x00" in norm or any(part in {".", ".."} for part in norm.split("/")):
            return None
        root: str | None = None
        relative = ""
        if norm.startswith("/mount/") and self.mount_dir:
            root = self.mount_dir
            relative = norm[len("/mount/"):]
        else:
            for folder in _RUN_WRITEBACK_FOLDERS:
                prefix = f"/{folder}/"
                if norm.startswith(prefix) and self.run_dir:
                    root = os.path.join(self.run_dir, folder)
                    relative = norm[len(prefix):]
                    break
        if not root or not relative:
            return None
        root_real = os.path.realpath(root)
        target = os.path.realpath(os.path.join(root_real, *relative.split("/")))
        if not target.startswith(root_real + os.sep):
            return None
        return norm, target

    async def mirror_vfs_delete(self, path: str) -> bool:
        """Remove a durable VFS path/prefix from this live mounted workspace."""
        resolved = self._external_vfs_target(path)
        if self.closed or resolved is None:
            return False
        norm, target = resolved

        def _delete() -> None:
            if os.path.islink(target) or os.path.isfile(target):
                os.unlink(target)
            elif os.path.isdir(target):
                shutil.rmtree(target)

        async with self._external_vfs_lock:
            await asyncio.to_thread(_delete)
        now = time.monotonic()
        self.last_used = now
        if _session_lifecycle_state(self) == "hibernated":
            self._hibernated_at = now
        logger.info("agent_session_mirror_vfs_delete_done", wf_id=self.wf_id, path=norm)
        return True

    async def mirror_vfs_rename(self, old_path: str, new_path: str) -> bool:
        """Project a durable file/folder rename into the live mounted workspace."""
        old_resolved = self._external_vfs_target(old_path)
        new_resolved = self._external_vfs_target(new_path)
        if self.closed or old_resolved is None or new_resolved is None:
            return False
        old_norm, old_target = old_resolved
        new_norm, new_target = new_resolved
        if old_target == new_target:
            return True

        def _rename() -> None:
            if not os.path.lexists(old_target):
                return
            if os.path.isdir(old_target) and not os.path.islink(old_target):
                # Durable folder rename merges its child rows with an existing
                # destination prefix. Reproduce that shape instead of nesting
                # the old directory under the destination as shutil.move does.
                for root, dirs, files in os.walk(old_target):
                    relative = os.path.relpath(root, old_target)
                    destination_root = (
                        new_target if relative == "." else os.path.join(new_target, relative)
                    )
                    os.makedirs(destination_root, exist_ok=True)
                    for name in files:
                        source = os.path.join(root, name)
                        destination = os.path.join(destination_root, name)
                        os.makedirs(os.path.dirname(destination), exist_ok=True)
                        os.replace(source, destination)
                    for name in dirs:
                        os.makedirs(os.path.join(destination_root, name), exist_ok=True)
                shutil.rmtree(old_target)
            else:
                os.makedirs(os.path.dirname(new_target), exist_ok=True)
                os.replace(old_target, new_target)

        async with self._external_vfs_lock:
            await asyncio.to_thread(_rename)
        now = time.monotonic()
        self.last_used = now
        if _session_lifecycle_state(self) == "hibernated":
            self._hibernated_at = now
        logger.info(
            "agent_session_mirror_vfs_rename_done",
            wf_id=self.wf_id,
            old_path=old_norm,
            new_path=new_norm,
        )
        return True

    async def acknowledge_external_vfs_commit(
        self,
        path: str,
        data: bytes,
    ) -> bool:
        """Advance the live mirror after a host-side semantic VFS commit.

        The path is fenced from ordinary turn-end writeback before attempting
        the mirror. A failed mirror marks the session for replacement, so a
        stale resident filesystem can never roll the durable revision back.
        """
        norm = "/" + path.strip("/")
        self._external_vfs_fenced_paths.add(norm)
        if self.closed:
            self._requires_rehydrate = True
            return False
        target: str | None = None
        if norm.startswith("/mount/") and self.mount_dir:
            rel = norm[len("/mount/"):]
            target = os.path.join(self.mount_dir, *rel.split("/"))
        else:
            for folder in _RUN_WRITEBACK_FOLDERS:
                prefix = f"/{folder}/"
                if norm.startswith(prefix) and self.run_dir:
                    rel = norm[len(prefix):]
                    target = os.path.join(self.run_dir, folder, *rel.split("/"))
                    break
        if not target:
            self._requires_rehydrate = True
            return False

        def _replace() -> None:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".vibecanvas-external-",
                dir=os.path.dirname(target),
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise

        try:
            await asyncio.to_thread(_replace)
        except Exception:
            self._requires_rehydrate = True
            logger.warning(
                "agent_external_vfs_commit_ack_failed",
                wf_id=self.wf_id,
                path=norm,
                exc_info=True,
            )
            return False
        self.last_used = time.monotonic()
        return True

    async def fence_external_vfs_path(self, path: str) -> bool:
        """Fence one path before a host-side compare-and-swap commit.

        Waiting on the session lock drains any writeback that already passed
        its fence check. Once this returns, later writebacks skip the path, so
        an older resident ``/data`` snapshot cannot overwrite the revision that
        the host is about to commit.
        """
        norm = "/" + path.strip("/")
        async with self._external_vfs_lock:
            self._external_vfs_fenced_paths.add(norm)
            if self.closed:
                self._requires_rehydrate = True
                return False
            self.last_used = time.monotonic()
            return True

    async def _sync_mount_folder(self) -> int:
        """Write the materialized user-level ``/mount`` back to durable VFS."""
        if not self.mount_dir or not self.mount_scope_id or not os.path.isdir(self.mount_dir):
            return 0
        return await persist_user_mount(
            source=self.mount_dir,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
        )

    async def _sync_run_folder(self, folder: str) -> int:
        """Diff-and-upsert one run-dir subfolder (``{run_dir}/{folder}``) to the
        VFS prefix ``/{folder}/`` using additive last-writer-wins semantics,
        without a baseline (the run directory is fresh per session, so
        every file present is new this session). Returns the count synced."""
        if not self.run_dir:
            return 0
        sub = os.path.join(self.run_dir, folder)
        if not os.path.isdir(sub):
            return 0

        def _collect() -> list[tuple[str, bytes]]:
            out: list[tuple[str, bytes]] = []
            for root, _dirs, files in os.walk(sub):
                for name in files:
                    fp = os.path.join(root, name)
                    rel = os.path.relpath(fp, sub)
                    try:
                        with open(fp, "rb") as f:
                            out.append((rel, f.read()))
                    except OSError:
                        logger.warning("agent_run_folder_read_failed",
                                       wf_id=self.wf_id, folder=folder, rel=rel,
                                       exc_info=True)
            # Persist empty-leaf dirs via the hidden sentinel (0-byte artifact).
            for rel_dir in _empty_leaf_dirs(sub):
                out.append((rel_dir + "/" + DIR_KEEP_SENTINEL, b""))
            return out

        collected = await asyncio.to_thread(_collect)
        if not collected:
            return 0
        synced = 0
        async with short_session_scope(tenant_id=self.tenant_id) as s:
            repo = VfsRepo(s, object_store=get_object_store())
            for rel, data in collected:
                vfs_path = f"/{folder}/" + rel.replace(os.sep, "/")
                try:
                    async with self._external_vfs_lock:
                        if vfs_path in self._external_vfs_fenced_paths:
                            continue
                        await repo.upsert_artifact_bytes(
                            wf_id=self.wf_id,
                            tenant=self.tenant_id,
                            path=vfs_path,
                            data=data,
                            content_type=_guess_ct(rel, data),
                        )
                    synced += 1
                except Exception:  # fail-soft per file
                    logger.warning("agent_run_folder_file_failed", wf_id=self.wf_id,
                                   path=vfs_path, exc_info=True)
        return synced

    def schedule_writeback(self) -> None:
        """Start a background diff-writeback if none is running; coalesce extra
        requests into a single pending re-run.

        Fire-and-forget (NOT awaited): the agent turn boundary calls this so the
        conversation is never blocked on the durable-VFS sync. The actual sync
        (:meth:`writeback_vfs`) runs UNDER ``self._lock`` inside
        :meth:`_run_writeback`, so it serializes against ``run_code`` (one
        store-touch at a time per session). MUST NOT be called while already
        holding ``self._lock`` (would re-enter the lock → deadlock); the turn
        boundary holds no session lock, so this is safe."""
        if self.closed:
            return
        if self._wb_task and not self._wb_task.done():
            self._wb_pending = True  # coalesce: exactly one re-run after the current
            return
        self._wb_task = asyncio.create_task(self._run_writeback())

    async def _run_writeback(self) -> None:
        """Run one diff-writeback under the session lock, then chain a single
        coalesced re-run if one was requested while it was in flight. The lock is
        the SAME one ``run_code`` takes, so writeback vs script runs serialize."""
        self._begin_activity()
        try:
            async with self._lock:
                try:
                    await self.writeback_vfs()
                except Exception:  # fail-soft: this is a fire-and-forget task that
                    # may never be drained, so a raised exception would surface as
                    # "Task exception was never retrieved" and the failure would be
                    # lost. ``writeback_vfs`` is fail-soft today; guard it here too
                    # so a future regression can't leak an un-retrieved task error
                    # (mirrors close()'s guard around its final writeback).
                    logger.warning("agent_async_writeback_failed", wf_id=self.wf_id,
                                   exc_info=True)
        finally:
            self._end_activity()
            # Drain invariant (load-bearing): every spawn publishes the new task to
            # ``self._wb_task`` SYNCHRONOUSLY here — there is NO ``await`` between
            # ``create_task`` and this assignment. That is what lets
            # ``drain_writeback`` re-read ``self._wb_task`` each loop iteration and
            # reliably catch a coalesced re-run chained at task completion. A future
            # edit that awaits between create_task and the assignment would break
            # drain.
            if self._wb_pending and not self.closed:
                self._wb_pending = False
                self._wb_task = asyncio.create_task(self._run_writeback())

    async def drain_writeback(self) -> None:
        """Await any in-flight + pending writeback (used before teardown). The
        loop re-checks because ``_run_writeback`` may chain a coalesced re-run as
        the current task finishes — we await each until none is outstanding."""
        while self._wb_task and not self._wb_task.done():
            await self._wb_task

    async def close(self) -> None:
        """Retire this session: drain any async writeback, final write-back, then
        stop every live process, remove any checkpoint image, and flag closed.
        Durable VFS and Runtime Volumes remain; daemon-private materializations
        and rebuildable overlays follow their existing ownership policies.

        Drains BEFORE setting ``closed`` so an in-flight ``schedule_writeback``
        run completes (can't be torn down mid-write); the final explicit
        ``writeback_vfs`` then captures anything written after the last schedule."""
        async with self._transition_lock:
            await self._close_once()

    async def _close_once(self) -> None:
        """Release one session while holding the lifecycle transition lock."""
        if self.closed or self._lifecycle_state == SessionLifecycleState.CLOSED.value:
            return
        wf_id = getattr(self, "wf_id", "<uninitialized>")
        # Drain while the session is still WARM. A scheduled writeback task is
        # created synchronously but may not have entered ``_run_writeback`` yet;
        # transitioning to RELEASING first makes its activity lease reject the
        # task and can silently skip the final durable sync. There is no await
        # between a completed drain and the transition below, so another task
        # cannot be scheduled into that boundary on this event loop. The outer
        # transition lock also excludes hibernate/restore/release races.
        try:
            await self.drain_writeback()
        except Exception:  # pragma: no cover - fail-soft
            logger.warning("agent_close_drain_failed", wf_id=wf_id,
                           exc_info=True)
        if self._lifecycle_state != SessionLifecycleState.RELEASING.value:
            self._transition_lifecycle(SessionLifecycleState.RELEASING)
        try:
            await self.writeback_vfs()
        except Exception:  # pragma: no cover - fail-soft
            logger.warning("agent_close_writeback_failed", wf_id=wf_id,
                           exc_info=True)
        # Runtime-owned files already live on the durable Chat Runtime Volume.
        # Retiring the process must not serialize, copy, or delete that volume.
        try:
            async with self._lock:
                await self._stop_agent_runtime_locked()
        except Exception:  # pragma: no cover - fail-soft
            logger.warning("agent_runtime_stop_failed", wf_id=wf_id,
                           exc_info=True)
        runtime_volume = getattr(self, "runtime_volume", None)
        if runtime_volume is not None:
            try:
                await asyncio.to_thread(
                    get_chat_runtime_volume_provider().release,
                    runtime_volume,
                )
            except Exception:  # pragma: no cover - durability failure is logged
                logger.warning(
                    "agent_runtime_volume_release_failed",
                    wf_id=wf_id,
                    volume_id=runtime_volume.volume_id,
                    exc_info=True,
                )
        # Task 4b-ii — tear down the warm file-op worker (a long-lived gVisor
        # process); ``stop()`` is sync → offload. Fail-soft: a teardown failure
        # must never raise out of close(). ``getattr`` guards a bare session
        # (constructed via ``__new__`` in writeback-only tests) that never set
        # ``_fileop_pool``.
        fileop_pool = getattr(self, "_fileop_pool", None)
        if fileop_pool is not None:
            try:
                await asyncio.to_thread(fileop_pool.stop)
            except Exception:  # pragma: no cover - fail-soft
                logger.warning("agent_fileop_pool_stop_failed", wf_id=wf_id,
                               exc_info=True)
            self._fileop_pool = None
        snapshot_dir = getattr(self, "_snapshot_dir", None)
        if snapshot_dir:
            try:
                await asyncio.to_thread(self._remove_snapshot_tree, snapshot_dir)
            except Exception:  # pragma: no cover - fail-closed path cleanup log
                logger.warning(
                    "sandbox_session_snapshot_cleanup_failed",
                    wf_id=wf_id,
                    exc_info=True,
                )
            self._snapshot_dir = None
            self._serve_snapshot = None
        projection_root = getattr(self, "materialized_projection_root", None)
        if projection_root:
            await asyncio.to_thread(shutil.rmtree, projection_root, True)
        else:
            run_dir = getattr(self, "run_dir", None)
            if run_dir:
                store = get_object_store()
                try:
                    await asyncio.to_thread(
                        store.release_materialized_prefix,
                        f"run/{self.tenant_id}/{self.wf_id}",
                        run_dir,
                    )
                except (NotImplementedError, ValueError):
                    logger.warning(
                        "agent_workspace_materialization_release_failed",
                        wf_id=wf_id,
                        exc_info=True,
                    )
        await asyncio.to_thread(remove_user_mount, getattr(self, "mount_dir", None))
        self.mount_dir = None
        self.closed = True
        self._transition_lifecycle(SessionLifecycleState.CLOSED)


class SandboxManager:
    """Process-singleton registry of resident per-workflow sandbox sessions.

    Lazy-creates a :class:`SandboxSession` on first ``get_session`` for a
    ``(tenant_id, wf_id)`` and reuses it on later calls; evicts the LRU session
    when the resident count would exceed ``max_resident``; reaps idle sessions via
    :meth:`sweep_idle`. The registry lock serializes create/evict/touch."""

    def __init__(self, *, max_resident: int, idle_ttl_s: int) -> None:
        self.max_resident = max(1, int(max_resident))
        self.idle_ttl_s = idle_ttl_s
        self.warm_idle_ttl_s = int(
            getattr(config, "sandbox_warm_idle_ttl_s", idle_ttl_s)
        )
        self.snapshot_idle_ttl_s = int(
            getattr(config, "sandbox_snapshot_idle_ttl_s", idle_ttl_s)
        )
        self.snapshot_sessions = (
            getattr(config, "sandbox_resident_mode", "coldboot") == "snapshot"
        )
        self._sessions: dict[tuple[str, str], SandboxSession] = {}
        self._closed_markers: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()
        self._close_tasks: set[asyncio.Task] = set()
        self._shutdown = False

    async def operational_snapshot(self) -> dict[str, int]:
        """Return bounded, content-free stats for the current API worker."""
        async with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if not session.closed
            ]
            warm_sessions = [
                session
                for session in sessions
                if _session_lifecycle_state(session) == "warm"
            ]
            return {
                "resident": len(warm_sessions),
                "hibernated": sum(
                    1
                    for session in sessions
                    if _session_lifecycle_state(session) == "hibernated"
                ),
                "capacity": self.max_resident,
                "busy": sum(
                    1 for session in warm_sessions
                    if _session_inflight_operations(session) > 0
                ),
                "resident_leases": sum(
                    1 for session in warm_sessions
                    if getattr(session, "lease", "interactive") == "resident"
                ),
                "pending_closes": len(self._close_tasks),
            }

    async def prewarm_base_fileops(self) -> dict[str, int | str]:
        """Boot and verify the shared base file-operation runtime once.

        The probe uses only daemon-created temporary roots. It does not acquire
        a tenant session, mount VFS/user data, create an overlay owned by a
        customer, or inject credentials. The ordinary ``SandboxSession``
        ``prewarm_fileops`` path remains the single implementation being
        exercised; this startup gate merely pays its base gVisor/Python cost
        before API traffic is admitted.
        """
        started = time.perf_counter()
        root = tempfile.mkdtemp(prefix="vcsbx-base-prewarm-")
        run_dir = os.path.join(root, "workspace")
        overlay_dir = os.path.join(root, "overlay")
        for folder in _RUN_WRITEBACK_FOLDERS:
            os.makedirs(os.path.join(run_dir, folder), mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(overlay_dir, "py"), mode=0o700, exist_ok=True)
        session = SandboxSession(
            tenant_id="00000000-0000-0000-0000-000000000000",
            wf_id="sandboxd-base-prewarm",
            run_dir=run_dir,
            overlay_dir=overlay_dir,
            provider=get_sandbox_provider(),
            base_binds=_workflow_python_binds(),
            expose_run=False,
            pool_runs_root=root,
        )
        snapshot_dir: str | None = None
        try:
            await session.prewarm_fileops()
            pool = session._fileop_pool
            if pool is None:
                raise RuntimeError("base file-operation worker did not start")
            result = await asyncio.to_thread(
                pool.submit_fileop,
                {
                    "op": "exec",
                    "command": (
                        "set -eu; for tool in "
                        + " ".join(_SANDBOX_BASELINE_TOOLS)
                        + "; do command -v \"$tool\" >/dev/null; done; "
                        "python -c 'import importlib,json,sys; "
                        "[importlib.import_module(module) for module in "
                        + json.dumps(_SANDBOX_BASELINE_PYTHON_MODULES)
                        + "]; "
                        "print(json.dumps({\"ok\": sys.version_info[:2] >= (3, 11)}))'"
                    ),
                    "cwd": "/",
                    "timeout": 30,
                },
                timeout=45,
            )
            if int(result.get("exit_code", 1)) != 0 or '"ok": true' not in str(
                result.get("stdout") or ""
            ).lower():
                logger.error(
                    "sandbox_base_fileop_verification_failed",
                    exit_code=result.get("exit_code"),
                    stdout=str(result.get("stdout") or "")[:1000],
                    stderr=str(result.get("stderr") or "")[:1000],
                )
                raise RuntimeError("base file-operation verification failed")
            if self.snapshot_sessions:
                snapshot_root = SandboxSession._snapshot_store_root(
                    SnapshotKind.BASELINE
                )
                snapshot_dir = tempfile.mkdtemp(
                    prefix="startup-probe-", dir=snapshot_root
                )
                os.chmod(snapshot_dir, 0o700)
                snapshot = await asyncio.to_thread(
                    pool.checkpoint,
                    image_dir=os.path.join(snapshot_dir, "image"),
                    fingerprint="startup-probe",
                    kind=SnapshotKind.BASELINE.value,
                )
                await asyncio.to_thread(pool.restore, snapshot)
                restored = await asyncio.to_thread(
                    pool.submit_fileop,
                    {
                        "op": "exec",
                        "command": "python -c 'print(\"snapshot-restored\")'",
                        "cwd": "/",
                        "timeout": 30,
                    },
                    timeout=45,
                )
                if (
                    int(restored.get("exit_code", 1)) != 0
                    or "snapshot-restored" not in str(restored.get("stdout") or "")
                ):
                    raise RuntimeError("snapshot restore verification failed")
                # The one-shot Workflow baseline must also restore against new
                # host sources for both /runs and /work. Acquire twice so the
                # second probe necessarily uses the cached image with different
                # directories; this catches unsupported mount substitution at
                # startup rather than during the first scheduled job.
                from vibecanvas_api.services.sandbox.lifecycle import SnapshotLifecycle

                workflow_lifecycle = SnapshotLifecycle()
                for index in range(2):
                    probe_runs = os.path.join(root, f"workflow-probe-{index}")
                    os.makedirs(probe_runs, mode=0o700, exist_ok=True)
                    handle = await asyncio.to_thread(
                        workflow_lifecycle.acquire,
                        runs_root=probe_runs,
                        concurrency=1,
                        tenant=None,
                        extra_rw_binds=None,
                    )
                    await asyncio.to_thread(workflow_lifecycle.release, handle)
                await self._prewarm_agent_runtime_baselines(root, session.provider)
            return {
                "status": "snapshot-ready" if self.snapshot_sessions else "ready",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        finally:
            pool = session._fileop_pool
            session._fileop_pool = None
            if pool is not None:
                await asyncio.to_thread(pool.stop)
            if snapshot_dir is not None:
                await asyncio.to_thread(
                    SandboxSession._remove_snapshot_tree, snapshot_dir
                )
            await asyncio.to_thread(shutil.rmtree, root, True)

    async def _prewarm_agent_runtime_baselines(
        self,
        root: str,
        provider: object,
    ) -> None:
        """Build and connection-test clean installed Runtime boot images."""

        from .agent_runtime_snapshot import ensure_agent_runtime_baseline

        os.makedirs(config.agent_runtime_root, mode=0o700, exist_ok=True)

        def build_probe(
            runtime_type: str,
            *,
            probe_root: str,
            persistent_root: str,
        ) -> tuple[
            SandboxSession,
            list[tuple[str, str]],
            list[str | tuple[str, str]],
            dict[str, str],
        ]:
            run_dir = os.path.join(probe_root, "workspace")
            overlay_dir = os.path.join(probe_root, "overlay")
            mount_dir = os.path.join(probe_root, "mount")
            # runsc annotates overlay-backed bind mounts during checkpoint and
            # validates those options during restore. Real Skills and Codex
            # Runtime Volumes live under AGENT_RUNTIME_ROOT, so bootstrap
            # substitutes must use that same filesystem rather than /tmp.
            skills_dir = os.path.join(persistent_root, "skills")
            # Chat Runtime Volumes are materialized under the projection root
            # (normally /tmp), unlike Skills/auth. Preserve that mixed parent /
            # child mount profile because runsc validates both independently.
            runtime_dir = os.path.join(probe_root, "runtime")
            for folder in _RUN_WRITEBACK_FOLDERS:
                os.makedirs(os.path.join(run_dir, folder), mode=0o700, exist_ok=True)
            os.makedirs(os.path.join(overlay_dir, "py"), mode=0o700, exist_ok=True)
            os.makedirs(mount_dir, mode=0o700, exist_ok=True)
            os.makedirs(skills_dir, mode=0o700, exist_ok=True)
            if runtime_dir:
                codex_home = os.path.join(runtime_dir, ".codex")
                os.makedirs(codex_home, mode=0o700, exist_ok=True)
                placeholder = os.path.join(codex_home, "auth.json")
                descriptor = os.open(
                    placeholder,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                os.close(descriptor)
            probe = SandboxSession(
                tenant_id="00000000-0000-0000-0000-000000000000",
                wf_id=f"runtime-bootstrap-{runtime_type}",
                run_dir=run_dir,
                overlay_dir=overlay_dir,
                provider=provider,
                mount_dir=mount_dir,
                runtime_dir=runtime_dir,
                skills_dir=skills_dir,
                base_binds=_workflow_python_binds(),
                expose_run=False,
                pool_runs_root=probe_root,
            )
            rw_binds, ro_binds, env = probe._agent_runtime_launch_spec(
                runtime_type=runtime_type,
                uses_codex_account=False,
            )
            return probe, rw_binds, ro_binds, env

        for runtime_type in config.agent_runtime_types:
            build_storage = tempfile.mkdtemp(
                prefix=f".runtime-bootstrap-{runtime_type}-",
                dir=config.agent_runtime_root,
            )
            verify_storage = tempfile.mkdtemp(
                prefix=f".runtime-verify-{runtime_type}-",
                dir=config.agent_runtime_root,
            )
            try:
                _probe, rw_binds, ro_binds, env = build_probe(
                    runtime_type,
                    probe_root=os.path.join(root, f"runtime-{runtime_type}"),
                    persistent_root=build_storage,
                )
                snapshot = await asyncio.to_thread(
                    ensure_agent_runtime_baseline,
                    provider,
                    runtime_type=runtime_type,
                    rw_binds=rw_binds,
                    ro_binds=ro_binds,
                    env_overrides=env,
                )
                verify_probe, verify_rw, verify_ro, verify_env = build_probe(
                    runtime_type,
                    probe_root=os.path.join(root, f"runtime-{runtime_type}-verify"),
                    persistent_root=verify_storage,
                )
                broker = BusBroker(
                    socket_path_for(f"runtime-bootstrap-verify:{runtime_type}")
                )
                handle = None
                try:
                    await broker.start()
                    handle = await asyncio.to_thread(
                        provider.launch_agent_runtime_bus,
                        run_id=f"runtime-bootstrap-verify-{runtime_type}",
                        bus_socket=broker.socket_path,
                        tenant=verify_probe.tenant_id,
                        extra_rw_binds=verify_rw,
                        extra_ro_binds=verify_ro,
                        env_overrides=verify_env,
                        snapshot=snapshot,
                    )
                    await SandboxSession._wait_for_agent_runtime_connection(
                        broker,
                        handle,
                        timeout=30.0,
                        restored_from_baseline=True,
                    )
                finally:
                    await broker.close()
                    if handle is not None:
                        await asyncio.to_thread(
                            provider.stop_run,
                            handle,
                            kill=True,
                        )
            finally:
                await asyncio.to_thread(shutil.rmtree, build_storage, True)
                await asyncio.to_thread(shutil.rmtree, verify_storage, True)

    async def run_mcp_probe(
        self,
        tenant_id: str,
        request: dict,
        *,
        timeout: float,
        allow_hosts: list[str],
    ) -> dict:
        """Run an untrusted MCP handshake inside a daemon-owned one-shot sandbox."""
        if not tenant_id:
            raise ValueError("tenant scope is required for an MCP probe")
        provider = get_sandbox_provider(trust="untrusted")
        return await asyncio.to_thread(
            provider.run_mcp_probe,
            request=request,
            timeout=timeout,
            allow_hosts=set(allow_hosts),
        )

    async def run_workflow_once(
        self,
        *,
        workflow_id: str,
        workflow: dict,
        inputs: dict,
        tenant_id: str,
        user_id: str,
        run_id: str,
        extra: dict | None,
        allow_hosts: list[str],
        requirements: str | None = None,
    ) -> dict:
        """Execute a one-shot deployment run entirely in sandboxd.

        The API sends logical data and scoped runtime capabilities only. VFS
        materialization, dependency-path resolution, user mount handling and
        gVisor lifecycle all happen on the sandbox node.
        """
        lib_overlay = None
        normalized_requirements = str(requirements or "").strip()
        if normalized_requirements:
            prepared = await self.ensure_workflow_dependencies(
                normalized_requirements,
            )
            if prepared["status"] != "ready":
                detail = prepared.get("error_log") or (
                    f"overlay status is {prepared['status']!r}"
                )
                raise RuntimeError(
                    "workflow dependency preparation failed "
                    f"({normalized_requirements!r}: {detail})"
                )
            lib_overlay = prepared.get("path")

        async with RunWorkspace(
            run_id,
            tenant_id,
            wf_id=workflow_id,
            user_id=user_id,
            keep_run=True,
        ) as workspace:
            run_dir = workspace.run_dir
            if not run_dir:
                raise RuntimeError(
                    "workflow execution requires an object store that can be "
                    "materialized on the sandbox node"
                )
            extra_path = os.path.join(run_dir, "__exec__", "extra.json")
            if extra:
                os.makedirs(os.path.dirname(extra_path), mode=0o700, exist_ok=True)
                with open(extra_path, "w", encoding="utf-8") as file:
                    json.dump(extra, file, ensure_ascii=False, default=str)
                os.chmod(extra_path, 0o600)
            try:
                async with sandbox_admission():
                    result = await asyncio.to_thread(
                        get_sandbox_provider().run_workflow,
                        run_dir=run_dir,
                        workflow=workflow,
                        inputs=inputs,
                        run_id=run_id,
                        tenant=tenant_id,
                        allow_hosts=set(allow_hosts),
                        lib_overlay=lib_overlay,
                        mount_dir=workspace.mount_dir,
                    )
            finally:
                # Credential capabilities are runtime-only. They must not enter
                # retained VFS run artifacts during RunWorkspace writeback.
                try:
                    os.remove(extra_path)
                except FileNotFoundError:
                    pass
            return {
                "final_outputs": result.final_outputs,
                "error_dict": result.error_dict,
                "execution_time": result.execution_time,
            }

    async def ensure_workflow_dependencies(self, requirements: str) -> dict:
        """Prepare one content-addressed dependency layer on the sandbox node.

        Package installation stays out of API and worker images. The rootful
        sandbox service owns the shared overlay volume and exposes only this
        narrow, wheel-only builder capability over its authenticated control
        channel; Workflow execution then mounts the published layer read-only.
        """
        prepared = await ensure_overlay(requirements)
        return {
            "overlay_key": prepared.overlay_key,
            "status": prepared.status,
            "path": prepared.path,
            "error_log": prepared.error_log,
        }

    async def _close_session_best_effort(self, session: SandboxSession, *, reason: str) -> None:
        timeout_s = max(0.1, float(config.sandbox_session_close_timeout_s))
        try:
            await asyncio.wait_for(session.close(), timeout=timeout_s)
        except asyncio.TimeoutError:
            session.closed = True
            logger.warning(
                "agent_session_close_timeout",
                wf_id=session.wf_id,
                reason=reason,
                timeout_s=timeout_s,
            )
        except Exception:  # pragma: no cover - fail-soft
            session.closed = True
            logger.warning(
                "agent_session_close_failed",
                wf_id=session.wf_id,
                reason=reason,
                exc_info=True,
            )

    def _schedule_close(self, session: SandboxSession, *, reason: str) -> None:
        task = asyncio.create_task(self._close_session_best_effort(session, reason=reason))
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    async def drain_background_closes(self) -> None:
        """Test/shutdown hook: wait for currently scheduled close tasks."""
        tasks = list(self._close_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def get_session(self, tenant_id: str, wf_id: str,
                          user_id: str | None = None,
                          expose_run: bool = True,
                          expose_runtime: bool = False,
                          lease: str = "interactive") -> SandboxSession:
        """Return the resident session for ``(tenant_id, wf_id)``, creating it
        (and evicting the LRU on overflow) on first use.

        ``user_id`` (when given) scopes the persistent pip OVERLAY to the USER —
        ``{agent_overlay_root}/{tenant}/{user_id}``, shared across ALL that user's
        workflows / chats — so installed packages are reused cross-workflow and the
        user's package storage is a single ``du`` of that dir (billing). ``None``
        falls back to the per-wf overlay. The session itself is still cached per
        ``(tenant, wf)``; multiple of a user's wf sessions just bind the one shared
        overlay dir."""
        acquire_started = time.perf_counter()
        key = (tenant_id, wf_id)
        # Restore outside the manager-wide registry lock. The Session's own
        # transition lock deduplicates concurrent restores for this Chat, while
        # unrelated Chats remain acquirable during checkpoint I/O.
        async with self._lock:
            restore_candidate = self._sessions.get(key)
            if (
                restore_candidate is not None
                and not restore_candidate.closed
                and _session_lifecycle_state(restore_candidate) != "warm"
            ):
                restore_candidate._hibernated_at = time.monotonic()
            else:
                restore_candidate = None
        if restore_candidate is not None:
            await restore_candidate.resume()
        async with self._lock:
            if self._shutdown:
                raise RuntimeError("sandbox manager is shutting down")
            existing = self._sessions.get(key)
            if existing is not None and not existing.closed:
                if getattr(existing, "_requires_rehydrate", False):
                    self._sessions.pop(key, None)
                    self._schedule_close(existing, reason="external_vfs_rehydrate")
                    existing = None
            if existing is not None and not existing.closed:
                if _session_lifecycle_state(existing) != "warm":
                    await existing.resume()
                if (
                    existing.expose_run == expose_run
                    and bool(existing.runtime_dir) == expose_runtime
                ):
                    existing.last_used = time.monotonic()
                    if lease == "resident":
                        existing.lease = "resident"
                    self._closed_markers.pop(key, None)
                    logger.info(
                        "agent_sandbox_session_acquired",
                        wf_id=wf_id,
                        cache_status="warm",
                        expose_run=expose_run,
                        expose_runtime=expose_runtime,
                        elapsed_ms=int(
                            (time.perf_counter() - acquire_started) * 1000
                        ),
                    )
                    return existing
                self._sessions.pop(key, None)
                self._schedule_close(existing, reason="rebuild")
            # Make room (LRU evict) BEFORE building the new one.
            while sum(
                1
                for loaded in self._sessions.values()
                if _session_lifecycle_state(loaded) == "warm"
            ) >= self.max_resident:
                candidates = {
                    k: s for k, s in self._sessions.items()
                    if getattr(s, "lease", "interactive") != "resident"
                    and _session_lifecycle_state(s) == "warm"
                    and _session_inflight_operations(s) == 0
                }
                if not candidates:
                    raise RuntimeError("resident sandbox capacity is full")
                victim_key = min(
                    candidates,
                    key=lambda k: candidates[k].last_used,
                )
                victim = self._sessions.pop(victim_key)
                self._schedule_close(victim, reason="evict")
            session = await self._build_session(
                tenant_id,
                wf_id,
                user_id=user_id,
                expose_run=expose_run,
                expose_runtime=expose_runtime,
            )
            session.lease = lease if lease in {"interactive", "resident"} else "interactive"
            self._sessions[key] = session
            self._closed_markers.pop(key, None)
            logger.info(
                "agent_sandbox_session_acquired",
                wf_id=wf_id,
                cache_status="cold",
                expose_run=expose_run,
                expose_runtime=expose_runtime,
                elapsed_ms=int(
                    (time.perf_counter() - acquire_started) * 1000
                ),
            )
            return session

    async def get_loaded_session(
        self, tenant_id: str, wf_id: str,
    ) -> SandboxSession | None:
        """Return a live local session without creating or rebuilding one.

        Runtime control delivery is best-effort after its database transition.
        A request handled by another API worker must not cold-boot a duplicate
        sandbox merely to discover that the original worker owns the broker.
        """
        async with self._lock:
            session = self._sessions.get((tenant_id, wf_id))
            if session is None or session.closed:
                return None
            if _session_lifecycle_state(session) != "warm":
                await session.resume()
            session.last_used = time.monotonic()
            return session

    async def set_session_lease(
        self,
        tenant_id: str,
        wf_id: str,
        lease: str,
    ) -> bool:
        """Pin or return a loaded session to its ordinary idle-TTL policy."""

        normalized = lease if lease in {"interactive", "resident"} else "interactive"
        async with self._lock:
            session = self._sessions.get((tenant_id, wf_id))
            if session is None or session.closed:
                return False
            session.lease = normalized
            session.last_used = time.monotonic()
            return True

    async def status(self, tenant_id: str, wf_id: str) -> dict:
        """Return resident-session status without creating a sandbox.

        ``idle`` means no resident session is loaded. ``running`` means the
        session has been lazily attached by a tool and is still within the idle
        TTL. ``closed`` is set by explicit user release and is cleared on the
        next lazy ``get_session``.
        """
        key = (tenant_id, wf_id)
        now = time.monotonic()
        observed_at_unix_s = time.time()
        async with self._lock:
            session = self._sessions.get(key)
            if session is not None and not session.closed:
                lifecycle_state = _session_lifecycle_state(session)
                if lifecycle_state == "hibernated":
                    hibernated_at = getattr(session, "_hibernated_at", None) or now
                    idle_elapsed = max(0.0, now - hibernated_at)
                    ttl_s = float(self.snapshot_idle_ttl_s)
                    return {
                        "status": "hibernated",
                        "lifecycle_state": lifecycle_state,
                        "lease": getattr(session, "lease", "interactive"),
                        "activity_state": "idle",
                        "idle_elapsed_s": idle_elapsed,
                        "idle_for_s": idle_elapsed,
                        "ttl_phase": "snapshot_retention",
                        "ttl_s": ttl_s,
                        "ttl_paused": False,
                        "ttl_remaining_s": max(0.0, ttl_s - idle_elapsed),
                        "next_transition": "release",
                        "observed_at_unix_s": observed_at_unix_s,
                        "resources": _session_resource_status(session),
                    }
                if lifecycle_state in {"hibernating", "restoring", "releasing"}:
                    return {
                        "status": lifecycle_state,
                        "lifecycle_state": lifecycle_state,
                        "lease": getattr(session, "lease", "interactive"),
                        "activity_state": "busy",
                        "idle_elapsed_s": 0.0,
                        "idle_for_s": 0.0,
                        "ttl_phase": None,
                        "ttl_s": None,
                        "ttl_paused": True,
                        "ttl_remaining_s": None,
                        "next_transition": {
                            "hibernating": "hibernate",
                            "restoring": "warm",
                            "releasing": "release",
                        }[lifecycle_state],
                        "observed_at_unix_s": observed_at_unix_s,
                        "resources": _session_resource_status(session),
                    }
                if lifecycle_state == "snapshot_failed":
                    return {
                        "status": "snapshot_failed",
                        "lifecycle_state": lifecycle_state,
                        "lease": getattr(session, "lease", "interactive"),
                        "activity_state": "unknown",
                        "idle_elapsed_s": None,
                        "idle_for_s": None,
                        "ttl_phase": None,
                        "ttl_s": None,
                        "ttl_paused": True,
                        "ttl_remaining_s": None,
                        "next_transition": None,
                        "observed_at_unix_s": observed_at_unix_s,
                        "error": getattr(session, "_snapshot_error", None),
                        "resources": _session_resource_status(session),
                    }
                observation = session.observe_activity(now=now)
                busy = bool(observation["busy"])
                resident = getattr(session, "lease", "interactive") == "resident"
                idle_elapsed = 0.0 if busy else max(0.0, now - session.last_used)
                active_ttl = (
                    self.warm_idle_ttl_s if self.snapshot_sessions else self.idle_ttl_s
                )
                ttl_paused = busy or resident or lifecycle_state == "hibernating"
                ttl_remaining = (
                    None
                    if ttl_paused
                    else max(0.0, float(active_ttl) - idle_elapsed)
                )
                return {
                    "status": (
                        "hibernating"
                        if lifecycle_state == "hibernating"
                        else "running"
                    ),
                    "lifecycle_state": lifecycle_state,
                    "lease": getattr(session, "lease", "interactive"),
                    "activity_state": "busy" if busy else "idle",
                    "inflight_operations": observation["inflight_operations"],
                    "idle_elapsed_s": idle_elapsed,
                    "idle_for_s": idle_elapsed,
                    "ttl_phase": (
                        "warm_idle" if self.snapshot_sessions else "idle_release"
                    ),
                    "ttl_s": float(active_ttl),
                    "ttl_paused": ttl_paused,
                    "ttl_remaining_s": ttl_remaining,
                    "next_transition": (
                        "hibernate" if self.snapshot_sessions else "release"
                    ),
                    "observed_at_unix_s": observed_at_unix_s,
                    "resources": _session_resource_status(session),
                }
            closed_at = self._closed_markers.get(key)
            if closed_at is not None:
                return {
                    "status": "closed",
                    "lifecycle_state": "closed",
                    "activity_state": "idle",
                    "idle_elapsed_s": None,
                    "idle_for_s": None,
                    "ttl_phase": None,
                    "ttl_s": None,
                    "ttl_paused": True,
                    "ttl_remaining_s": None,
                    "next_transition": None,
                    "observed_at_unix_s": observed_at_unix_s,
                    "closed_for_s": max(0.0, now - closed_at),
                    "resources": _released_resource_status(
                        lifecycle_state="closed"
                    ),
                }
        return {
            "status": "idle",
            "lifecycle_state": "released",
            "activity_state": "idle",
            "idle_elapsed_s": None,
            "idle_for_s": None,
            "ttl_phase": None,
            "ttl_s": None,
            "ttl_paused": True,
            "ttl_remaining_s": None,
            "next_transition": None,
            "observed_at_unix_s": observed_at_unix_s,
            "resources": _released_resource_status(
                lifecycle_state="released"
            ),
        }

    async def close_session(self, tenant_id: str, wf_id: str) -> dict:
        """Explicitly release a resident session and mark it closed for UI state.

        The session is removed from the live registry synchronously, then fully
        closed before the release response is returned.  A caller may start the
        same Chat again immediately after this boundary; returning while the old
        close task can still release its Runtime-volume materialization races the
        replacement sandbox and can remove its ``/runtime`` mount mid-startup.
        """
        key = (tenant_id, wf_id)
        victim = None
        async with self._lock:
            victim = self._sessions.pop(key, None)
            self._closed_markers[key] = time.monotonic()
        if victim is not None:
            await self._close_session_best_effort(victim, reason="manual_close")
        return await self.status(tenant_id, wf_id)

    async def checkpoint_session(self, tenant_id: str, wf_id: str) -> str:
        """Explicitly hibernate one quiescent interactive session."""
        if not self.snapshot_sessions:
            raise RuntimeError("configured SANDBOX_TYPE does not support snapshots")
        async with self._lock:
            session = self._sessions.get((tenant_id, wf_id))
            if session is None or session.closed:
                raise LookupError("sandbox session is not loaded")
            if getattr(session, "lease", "interactive") == "resident":
                raise RuntimeError("resident sandbox lease is still active")
            if _session_inflight_operations(session) != 0:
                raise RuntimeError("sandbox session is busy")
        await session.hibernate()
        snapshot = getattr(session, "_serve_snapshot", None)
        if snapshot is not None:
            return snapshot.fingerprint
        return hashlib.sha256(f"{tenant_id}\0{wf_id}\0metadata".encode()).hexdigest()

    async def close_tenant(self, tenant_id: str, *, reason: str = "tenant_purge") -> int:
        """Detach every locally-owned sandbox for an organization.

        This is intentionally process-local; durable authorization and resource
        deletion prevent another worker from acquiring a replacement. Existing
        remote sandboxes remain bounded by their ordinary TTL/provider lease.
        """
        victims: list[SandboxSession] = []
        async with self._lock:
            keys = [key for key in self._sessions if key[0] == tenant_id]
            for key in keys:
                victim = self._sessions.pop(key)
                self._closed_markers[key] = time.monotonic()
                victims.append(victim)
        for victim in victims:
            self._schedule_close(victim, reason=reason)
        await self.drain_background_closes()
        return len(victims)

    async def close_user(self, user_id: str, *, reason: str = "account_purge") -> int:
        """Detach this identity's sandboxes across every organization.

        Account deletion removes the personal tenant, but a user may also have
        resident Chat or debug sessions inside organization-owned workspaces.
        Those processes still contain user-scoped Runtime state and therefore
        must not survive identity erasure.
        """
        victims: list[SandboxSession] = []
        async with self._lock:
            keys = [
                key for key, session in self._sessions.items()
                if session.user_id == user_id
            ]
            for key in keys:
                victim = self._sessions.pop(key)
                self._closed_markers[key] = time.monotonic()
                victims.append(victim)
        for victim in victims:
            self._schedule_close(victim, reason=reason)
        await self.drain_background_closes()
        return len(victims)

    async def purge_user_storage(
        self,
        user_id: str,
        tenant_ids: list[str],
        personal_tenant_id: str,
    ) -> bool:
        """Remove identity-owned persistent Runtime trees on the sandbox host.

        API and worker containers intentionally do not mount these volumes in a
        service deployment. Account erasure must therefore cross the sandboxd
        control plane instead of deleting a same-named, container-local path.
        UUID canonicalization and parent checks keep the RPC narrowly scoped.
        """
        canonical_user_id = str(uuid.UUID(user_id))
        canonical_personal_tenant_id = str(uuid.UUID(personal_tenant_id))
        canonical_tenant_ids = {
            str(uuid.UUID(tenant_id)) for tenant_id in tenant_ids
        }
        canonical_tenant_ids.add(canonical_personal_tenant_id)

        def remove_user_directory(root: str, tenant_id: str) -> None:
            if not root:
                return
            resolved_root = os.path.realpath(root)
            tenant_directory = os.path.join(resolved_root, tenant_id)
            target = os.path.join(tenant_directory, canonical_user_id)
            if os.path.islink(tenant_directory) or os.path.islink(target):
                raise RuntimeError("sandbox storage purge refuses symbolic links")
            if not os.path.exists(target):
                return
            if os.path.commonpath((resolved_root, os.path.realpath(target))) != resolved_root:
                raise RuntimeError("sandbox user storage escaped configured root")
            shutil.rmtree(target)

        def remove_personal_tenant_directory(root: str) -> None:
            if not root:
                return
            resolved_root = os.path.realpath(root)
            target = os.path.join(resolved_root, canonical_personal_tenant_id)
            if os.path.islink(target):
                raise RuntimeError("sandbox storage purge refuses symbolic links")
            if not os.path.exists(target):
                return
            if (
                os.path.dirname(os.path.realpath(target)) != resolved_root
                or os.path.realpath(target) == resolved_root
            ):
                raise RuntimeError("sandbox tenant storage escaped configured root")
            shutil.rmtree(target)

        roots = {
            os.path.realpath(root)
            for root in (
                config.agent_overlay_root,
                config.agent_runtime_root,
                config.vfs_volume_root,
            )
            if root
        }
        for root in sorted(roots):
            for tenant_id in sorted(canonical_tenant_ids):
                await asyncio.to_thread(remove_user_directory, root, tenant_id)
            await asyncio.to_thread(remove_personal_tenant_directory, root)
        return True

    async def invalidate_codex_account_sessions(
        self,
        tenant_id: str,
        user_id: str,
    ) -> int:
        """Detach every live or hibernated Chat bound to this account.

        The persistent binding flag intentionally survives Runtime teardown.
        Otherwise an account disconnected while its Chat was hibernated could
        later restore stale account state and escape revocation.
        """
        victims: list[SandboxSession] = []
        async with self._lock:
            keys = [
                key
                for key, session in self._sessions.items()
                if key[0] == tenant_id
                and session.user_id == user_id
                and session._bound_runtime_uses_codex_account
            ]
            for key in keys:
                victims.append(self._sessions.pop(key))
        for victim in victims:
            self._schedule_close(victim, reason="codex_account_disconnected")
        return len(victims)

    async def mirror_vfs_write(self, tenant_id: str, wf_id: str, path: str,
                               data: bytes) -> bool:
        """Mirror a durable VFS write into an existing live session, if present.

        This is intentionally non-creating: VFS edits should not warm a sandbox
        by themselves. Returns whether a live session was updated.
        """
        key = (tenant_id, wf_id)
        async with self._lock:
            sessions = []
            direct = self._sessions.get(key)
            if direct is not None and not direct.closed:
                sessions.append(direct)
            if path.startswith("/mount/"):
                for (tenant, _sid), session in self._sessions.items():
                    if tenant != tenant_id or session.closed or session is direct:
                        continue
                    if getattr(session, "mount_scope_id", None) == wf_id:
                        sessions.append(session)
            if not sessions:
                return False
        updated = False
        errors = 0
        for session in sessions:
            try:
                updated = await session.mirror_vfs_write(path, data) or updated
            except Exception:  # pragma: no cover - fail-soft sync aid
                errors += 1
                logger.warning("agent_session_mirror_vfs_write_failed",
                               wf_id=wf_id, path=path, exc_info=True)
        if errors and not updated:
            return False
        return updated

    async def _mirror_vfs_path_operation(
        self,
        tenant_id: str,
        wf_id: str,
        path: str,
        method: str,
        *args,
    ) -> bool:
        """Apply a non-creating VFS path operation to every matching session."""
        key = (tenant_id, wf_id)
        async with self._lock:
            sessions = []
            direct = self._sessions.get(key)
            if direct is not None and not direct.closed:
                sessions.append(direct)
            if path.startswith("/mount/"):
                for (tenant, _sid), session in self._sessions.items():
                    if tenant != tenant_id or session.closed or session is direct:
                        continue
                    if getattr(session, "mount_scope_id", None) == wf_id:
                        sessions.append(session)
            if not sessions:
                return False
        updated = False
        for session in sessions:
            try:
                operation = getattr(session, method)
                updated = await operation(path, *args) or updated
            except Exception:  # pragma: no cover - fail-soft sync aid
                logger.warning(
                    f"agent_session_{method}_failed",
                    wf_id=wf_id,
                    path=path,
                    exc_info=True,
                )
        return updated

    async def mirror_vfs_delete(
        self, tenant_id: str, wf_id: str, path: str,
    ) -> bool:
        return await self._mirror_vfs_path_operation(
            tenant_id, wf_id, path, "mirror_vfs_delete",
        )

    async def mirror_vfs_rename(
        self, tenant_id: str, wf_id: str, old_path: str, new_path: str,
    ) -> bool:
        return await self._mirror_vfs_path_operation(
            tenant_id, wf_id, old_path, "mirror_vfs_rename", new_path,
        )

    async def _build_session(self, tenant_id: str, wf_id: str,
                             user_id: str | None = None,
                             expose_run: bool = True,
                             expose_runtime: bool = False) -> SandboxSession:
        """Materialize Chat/user VFS mounts and construct the session.

        ``build_run_context`` (blocking DB+ObjectStore+FS, run off-loop) gives
        the chat/workspace-owned ``run_dir`` for /data, /memory, and /logs. The per-agent
        ``overlay_dir`` (installed-pip scratch) is ensured under
        ``config.agent_overlay_root/{tenant}/{user_id or wf_id}`` — scoped to the USER
        (shared across their workflows) when a ``user_id`` is given, else per-wf.
        ``base_binds`` are the host sys.path RO binds so the engine entrypoint imports
        inside the sandbox."""
        total_started = time.perf_counter()
        logger.warning(
            "agent_sandbox_session_build_start",
            wf_id=wf_id,
            expose_run=expose_run,
        )
        stage_started = time.perf_counter()
        ctx = await asyncio.to_thread(
            build_run_context, wf_id, tenant_id)
        logger.warning(
            "agent_sandbox_session_build_stage_done",
            stage="chat_workspace_context",
            wf_id=wf_id,
            elapsed_ms=int((time.perf_counter() - stage_started) * 1000),
        )
        run_dir = ctx["run_dir"]
        projection_root = None
        pool_runs_root = os.path.dirname(run_dir) if run_dir else None
        store = get_object_store()
        if run_dir and not isinstance(store, FilesystemObjectStore):
            # S3 materialization returns an opaque temporary directory. Rehome
            # it below a logical scope name so the fixed /runs/<scope> protocol
            # remains provider-neutral and does not leak a host-generated path.
            safe_scope_id = _runtime_identity_component(wf_id, field="scope_id")
            projection_root = tempfile.mkdtemp(prefix="vcsbx-projection-")
            pool_runs_root = os.path.join(projection_root, "runs")
            os.makedirs(pool_runs_root, mode=0o700, exist_ok=True)
            projected_run_dir = os.path.join(pool_runs_root, safe_scope_id)
            shutil.move(run_dir, projected_run_dir)
            run_dir = projected_run_dir

        # Pre-create the workspace folders under run_dir so a bare write to
        # ``/data`` (etc.) just works without a manual ``mkdir -p`` first.
        for f in _RUN_WRITEBACK_FOLDERS:
            os.makedirs(os.path.join(run_dir, f), exist_ok=True)

        # Boot-hydrate: the run dir is fresh per (re)build, but the durable VFS
        # holds the agent's prior /data /memory /logs. Re-materialize them so an
        # LRU evict + rebuild does NOT lose the working FS (inverse of the run
        # write-back). Fail-soft — never block session creation. DB reads stay on
        # the loop; blocking writes are offloaded inside the helper.
        try:
            stage_started = time.perf_counter()
            await _hydrate_run_folders(run_dir, wf_id, tenant_id)
            logger.warning(
                "agent_sandbox_session_build_stage_done",
                stage="hydrate_chat_workspace",
                wf_id=wf_id,
                elapsed_ms=int((time.perf_counter() - stage_started) * 1000),
            )
        except Exception:  # pragma: no cover - fail-soft
            logger.warning("agent_hydrate_run_folders_failed", wf_id=wf_id,
                           tenant_id=tenant_id, exc_info=True)

        mount_scope_id = user_mount_scope_id(user_id)
        mount_dir = (run_dir.rstrip("/") + ".mount") if run_dir and mount_scope_id else None
        if mount_dir and mount_scope_id:
            try:
                stage_started = time.perf_counter()
                await hydrate_user_mount(
                    destination=mount_dir,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
                logger.warning(
                    "agent_sandbox_session_build_stage_done",
                    stage="hydrate_user_mount",
                    wf_id=wf_id,
                    mount_scope_id=mount_scope_id,
                    elapsed_ms=int((time.perf_counter() - stage_started) * 1000),
                )
            except Exception:  # pragma: no cover - fail-soft
                logger.warning(
                    "agent_hydrate_user_mount_failed",
                    wf_id=wf_id,
                    mount_scope_id=mount_scope_id,
                    tenant_id=tenant_id,
                    exc_info=True,
                )

        runtime_dir = None
        runtime_volume = None
        account_auth_path = None
        if user_id and expose_runtime:
            # Runtime state is isolated per Chat and backed by one directly
            # mounted POSIX volume. The provider may use a local encrypted disk,
            # CSI RWO volume, or a sandbox platform volume; no adapter-specific
            # checkpoint format participates in session startup or teardown.
            stage_started = time.perf_counter()
            runtime_volume = await asyncio.to_thread(
                get_chat_runtime_volume_provider().ensure,
                tenant_id=tenant_id,
                user_id=user_id,
                chat_scope_id=wf_id,
            )
            runtime_dir = runtime_volume.path
            logger.warning(
                "agent_sandbox_session_build_stage_done",
                stage="mount_chat_runtime_volume",
                wf_id=wf_id,
                volume_id=runtime_volume.volume_id,
                elapsed_ms=int((time.perf_counter() - stage_started) * 1000),
            )
            candidate_auth_path = codex_account_auth_file(
                tenant_id,
                user_id,
            )
            # Keep the expected path even when the account is connected after
            # this Chat session was created. It is validated and mounted only
            # for an explicit ``chatgpt_account`` Runtime request.
            account_auth_path = candidate_auth_path
            codex_home = os.path.join(runtime_dir, ".codex")
            os.makedirs(codex_home, mode=0o700, exist_ok=True)
            mount_target = os.path.join(codex_home, "auth.json")
            if not os.path.exists(mount_target):
                descriptor = os.open(
                    mount_target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                os.close(descriptor)

        skills_dir = (
            os.path.join(config.agent_runtime_root, tenant_id, user_id, ".skills-vfs")
            if user_id else None
        )
        if skills_dir:
            os.makedirs(skills_dir, mode=0o700, exist_ok=True)

        # Per-USER overlay (shared across the user's workflows) when user_id is given,
        # else per-wf. ``du`` of ``{agent_overlay_root}/{tenant}/{user_id}`` is the
        # user's installed-package storage (billing).
        overlay_dir = os.path.join(config.agent_overlay_root, tenant_id, user_id or wf_id)
        # The ``py`` subdir is the pip ``--target`` (PIP_TARGET) for the install path;
        # it is mounted rw at ``/opt/agent-overlay`` and ``/opt/agent-overlay/py`` is
        # prepended to PYTHONPATH so installed packages import inside the sandbox.
        os.makedirs(os.path.join(overlay_dir, "py"), exist_ok=True)

        provider = get_sandbox_provider()
        base_binds = _workflow_python_binds()
        session = SandboxSession(
            tenant_id=tenant_id,
            wf_id=wf_id,
            run_dir=run_dir,
            overlay_dir=overlay_dir,
            provider=provider,
            mount_dir=mount_dir,
            runtime_dir=runtime_dir,
            runtime_volume=runtime_volume,
            account_auth_file=account_auth_path,
            skills_dir=skills_dir,
            mount_scope_id=mount_scope_id,
            user_id=user_id,
            base_binds=base_binds,
            expose_run=expose_run,
            pool_runs_root=pool_runs_root,
            materialized_projection_root=projection_root,
        )
        logger.warning(
            "agent_sandbox_session_build_done",
            wf_id=wf_id,
            expose_run=expose_run,
            elapsed_ms=int((time.perf_counter() - total_started) * 1000),
        )
        return session

    async def sweep_idle(self) -> int:
        """Advance idle interactive sessions through hibernate and release."""
        now = time.monotonic()
        reaped = 0
        victims: list[SandboxSession] = []
        hibernate: list[SandboxSession] = []
        async with self._lock:
            # This is the dedicated lifecycle observation pass. It refreshes
            # the host monotonic silence clock from both daemon-owned leases
            # and the positive activity state published by each warm worker.
            for session in self._sessions.values():
                if (
                    not session.closed
                    and _session_lifecycle_state(session) == "warm"
                ):
                    session.observe_activity(now=now)
            if self.snapshot_sessions:
                stale = [
                    key
                    for key, session in self._sessions.items()
                    if getattr(session, "lease", "interactive") != "resident"
                    and _session_inflight_operations(session) == 0
                    and _session_lifecycle_state(session) == "hibernated"
                    and now - (getattr(session, "_hibernated_at", None) or now)
                    > self.snapshot_idle_ttl_s
                ]
                hibernate = [
                    session
                    for session in self._sessions.values()
                    if getattr(session, "lease", "interactive") != "resident"
                    and _session_inflight_operations(session) == 0
                    and _session_lifecycle_state(session) == "warm"
                    and now - session.last_used > self.warm_idle_ttl_s
                ]
            else:
                stale = [
                    key
                    for key, session in self._sessions.items()
                    if getattr(session, "lease", "interactive") != "resident"
                    and _session_inflight_operations(session) == 0
                    and now - session.last_used > self.idle_ttl_s
                ]
            for k in stale:
                victim = self._sessions.pop(k)
                self._closed_markers.pop(k, None)
                victims.append(victim)
                reaped += 1
        for victim in victims:
            self._schedule_close(victim, reason="idle_sweep")
        for session in hibernate:
            try:
                await session.hibernate()
            except Exception:
                # The session remains fail-closed as snapshot_failed. A later
                # acquire surfaces the failure instead of silently cold-booting.
                logger.error(
                    "sandbox_idle_hibernate_failed",
                    wf_id=session.wf_id,
                    exc_info=True,
                )
        return reaped

    async def shutdown(self) -> None:
        """Close every resident session and wait for pending close tasks.

        This is the process shutdown hook. It intentionally removes sessions
        from the registry before closing them so no new callers can acquire a
        stale session while the process is exiting.
        """
        victims: list[SandboxSession] = []
        async with self._lock:
            self._shutdown = True
            victims = list(self._sessions.values())
            self._sessions.clear()
            self._closed_markers.clear()
        for victim in victims:
            self._schedule_close(victim, reason="process_shutdown")
        await self.drain_background_closes()


_manager: object | None = None


def get_sandbox_manager():
    """Return the configured process singleton.

    Normal application processes receive a UDS-backed manager proxy.  The
    process-owning implementation is constructed directly by ``sandboxd`` so
    importing this factory there cannot accidentally recurse back into RPC.
    """
    global _manager
    if _manager is None:
        if config.sandbox_service_mode == "embedded":
            _manager = SandboxManager(
                max_resident=config.sandbox_max_resident,
                idle_ttl_s=config.sandbox_idle_ttl_s,
            )
        elif config.sandbox_service_mode == "service":
            from vibecanvas_api.services.sandbox.service import RemoteSandboxManager
            _manager = RemoteSandboxManager(
                config.sandbox_service_endpoint,
                connect_timeout_s=config.sandbox_service_connect_timeout_s,
            )
        else:
            raise RuntimeError(
                "SANDBOX_SERVICE_MODE must be 'service' or 'embedded'"
            )
    return _manager


def get_existing_sandbox_manager():
    """Return the process singleton only if it has already been created."""
    return _manager


def clear_sandbox_manager(expected: object | None = None) -> None:
    """Forget a manager that has completed process-lifecycle shutdown.

    The optional identity guard makes shutdown safe when application
    lifespans overlap during an in-process restart: an older lifespan cannot
    discard a newer manager.
    """
    global _manager
    if expected is not None and _manager is not expected:
        return
    _manager = None


def agent_overlay_dir(tenant_id: str, user_id: str) -> str:
    """The host dir holding ``user_id``'s installed pip packages under ``tenant_id``
    (shared across the user's workflows). Single source of the per-user overlay path
    for billing / GC — keep in sync with ``_build_session``."""
    return os.path.join(config.agent_overlay_root, tenant_id, user_id)


def agent_overlay_size_bytes(tenant_id: str, user_id: str) -> int:
    """Total bytes of ``user_id``'s installed-package overlay (0 if absent) — the
    per-user package storage to meter for billing."""
    total = 0
    for dirpath, _dirs, files in os.walk(agent_overlay_dir(tenant_id, user_id)):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:  # pragma: no cover - file vanished mid-walk
                pass
    return total
