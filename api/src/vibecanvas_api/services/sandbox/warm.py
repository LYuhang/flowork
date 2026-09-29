"""Warm gVisor workers for workflow runs and agent-visible sandbox jobs.

Warm pools boot ONE long-lived gVisor sandbox whose in-sandbox
``serve-parallel`` job server handles ``size`` concurrent jobs. That gives both
agent shell/file jobs and workflow jobs a normal "one Linux environment, many
commands" model without starting N runsc sandboxes for N-way concurrency.

Each warm sandbox binds the Object Store run root
(``{store_root}/run`` → ``/runs``) ONCE at boot; every tenant/run is a subpath
(``/runs/{tenant}/{run_id}``) so jobs share the same mounted workspace without
re-mounting. A second bind (``work_root`` → ``/work``) is the job channel.

Per ``submit`` the host:
1. ``classify_workflow`` — reject host/API nodes BEFORE the channel; only
   engine-native nodes enter this credential-free pool.
2. writes ``{store_root}/run/{tenant}/{run_id}/__exec__/{workflow,inputs}.json``
   into the bound run-tier (visible to the worker via the inode-shared bind),
3. drops ``{work}/inbox/{job_id}.json`` + an atomic ``{job_id}.ready`` marker,
4. polls ``{work}/outbox/{job_id}.done``, then reads ``result.json`` back.

Availability:
- **B3 hung-worker wedge:** jobs run through the parallel API serve loop;
  individual exec jobs carry their own timeout and a host-side timeout abandons
  that job without rebuilding the whole sandbox.
- **B4 queued-vs-running:** a job that times out while still QUEUED (no
  ``.taken``) must NOT kill the worker (another job may legitimately be
  running) — just clean its own markers + give up. Host bookkeeping is
  serialized with ``_lock``.

Every live pool receives one tenant-scoped, daemon-private projection. With the
local provider it is decrypted from VCOBJ2; with S3 it is a sync-down cache.
The durable ciphertext/object-store root and its cloud credentials are never
mounted into the sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
import time
from typing import AsyncIterator
from uuid import uuid4

import structlog

from ...config import config
from ..object_store import FilesystemObjectStore, get_object_store
from .gvisor import (
    EngineRunResult,
    ServeSnapshot,
    _workflow_python_binds,
    _workflow_python_env,
    _workflow_python_paths,
)
from .workflow_guard import classify_workflow

logger = structlog.get_logger(__name__)

_AGENT_SHELL_ENV = {
    # The sandbox rootfs is read-only. Package-backed stdio MCP launchers such
    # as npx/uvx need a writable home/cache even though the MCP itself is
    # ephemeral and chat-scoped.
    "HOME": "/tmp",
    "XDG_CACHE_HOME": "/tmp/.cache",
    "NPM_CONFIG_CACHE": "/tmp/npm-cache",
    "UV_CACHE_DIR": "/tmp/uv-cache",
    "PAGER": "cat",
    "MANPAGER": "cat",
    "GIT_PAGER": "cat",
    "LESS": "-R",
    "PIP_PROGRESS_BAR": "off",
    "TQDM_DISABLE": "1",
}


class WarmGvisorPool:
    """Warm gVisor execution pool.

    ``size`` is the in-sandbox job-server concurrency for both workflow and
    file/shell jobs. ``fileops=True`` adds clean user-visible writable mounts
    such as ``/data`` or ``/mount``; both modes use the same ``serve-parallel``
    entrypoint.
    """

    def __init__(
        self,
        *,
        provider,
        store_root: str,
        work_root: str,
        size: int = 1,
        poll_interval: float = 0.02,
        fileops: bool = False,
        fileop_binds: "list[tuple[str, str]] | None" = None,
        fileop_roots: "list[str] | None" = None,
        tenant: "str | None" = None,
        materialized_runs_root: "str | None" = None,
    ) -> None:
        self.provider = provider
        self.store_root = store_root
        self.work_root = work_root
        self.size = max(1, int(size or 1))
        self.poll_interval = poll_interval
        # Enforce cross-tenant isolation at the mount boundary:
        # when ``tenant`` is set, this pool binds ONLY ``{store_root}/run/{tenant}``
        # → /runs (the tenant SUBTREE), so another tenant's dir is not in the
        # sandbox's filesystem at all (ENOENT — physical isolation, not the
        # in-process jail). ``tenant=None`` keeps the legacy SHARED-root behavior
        # (binds the whole ``{store_root}/run`` → /runs, soft isolation only).
        self.tenant = tenant
        # Object-backed providers such as S3 hydrate a daemon-private local
        # projection instead of exposing a durable shared-filesystem root.
        # When supplied, this already tenant-scoped directory is the only tree
        # mounted at /runs.
        self.materialized_runs_root = materialized_runs_root
        # ``fileops=True`` means the session exposes clean file roots and submits
        # file/shell jobs. It still uses the same api ``serve-parallel`` worker as
        # workflow jobs; only the allowed roots differ.
        self.fileops = fileops
        # Task 4b-i — the agent's CLEAN mount points: a list of
        # ``(dest_mount, host_source)`` writable binds (e.g. ``("/data",
        # run_dir/data)``) appended to the
        # worker's ``/runs`` + ``/work`` binds. When set (fileops mode), the serve
        # loop confines file ops to EXACTLY these dest mounts (via
        # ``VIBECANVAS_FILEOP_ROOTS``), NOT ``/runs``. ``None`` → Task 4a behavior
        # (bind only ``/runs``, roots default to runs_root).
        self.fileop_binds = fileop_binds
        # Binds can contain runtime-private mounts (for example /runtime) that
        # the sandbox process needs but Agent filesystem tools must not access.
        # Root confinement therefore has its own explicit list.
        self.fileop_roots = fileop_roots
        # Serializes lifecycle/restart bookkeeping. ``size`` is the in-sandbox
        # job-server concurrency of ONE gVisor sandbox.
        self._lock = threading.Lock()
        self._handles: list = []
        # A host waiter may time out after the in-sandbox thread has already
        # claimed a file/tool job. Keep that job leased here until its terminal
        # marker appears; the sandbox-published state is useful observability,
        # but untrusted guest files are never the sole lifecycle authority.
        self._activity_lock = threading.Lock()
        self._abandoned_jobs: set[str] = set()
        # Proxy mode keeps one broker and one in-sandbox forward proxy for the
        # resident worker's lifetime. Workflow runs expand the host allowlist
        # before submission; Chat file/tool workers may opt into public egress.
        self._egress_loop_thread = None
        self._egress_socket: str | None = None
        self._egress_proxy_env: dict[str, str] = {}

    # -- paths -------------------------------------------------------------
    @property
    def _runs_root(self) -> str:
        # Per-tenant (T2): the bound subtree IS the tenant dir
        # (``{store_root}/run/{tenant}``) → /runs, so the gofer never serves any
        # OTHER tenant's tree. Shared (legacy): the whole ``{store_root}/run``.
        if self.materialized_runs_root is not None:
            return self.materialized_runs_root
        if self.tenant is not None:
            return os.path.join(self.store_root, "run", self.tenant)
        return os.path.join(self.store_root, "run")

    @property
    def _inbox(self) -> str:
        return self._slot_inbox(0)

    @property
    def _outbox(self) -> str:
        return self._slot_outbox(0)

    def _slot_work_root(self, slot: int) -> str:
        return self.work_root

    def _slot_inbox(self, slot: int) -> str:
        return os.path.join(self._slot_work_root(slot), "inbox")

    def _slot_outbox(self, slot: int) -> str:
        return os.path.join(self._slot_work_root(slot), "outbox")

    @property
    def _slot_count(self) -> int:
        return 1

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Boot the ONE warm worker (the cold boot + engine import is paid here,
        ONCE).

        B2: the bound path must be a daemon-private materialized filesystem
        projection. It may be backed by the encrypted local Object Store or a
        sync-down cache for S3; durable cloud credentials are never mounted.
        B1: ``{store_root}/run`` may not exist at pool-boot
        (``FilesystemObjectStore`` creates it lazily on the first run via
        ``materialize_prefix``) — make it (+ the work dirs) BEFORE ``run_serve``
        binds them.
        """
        # Direct/legacy callers must still prove a filesystem-backed stable
        # root. Object-backed deployments enter through SandboxSession, which
        # supplies an explicit daemon-owned materialized projection.
        if (
            self.materialized_runs_root is None
            and not isinstance(get_object_store(), FilesystemObjectStore)
        ):
            raise RuntimeError(
                "WarmGvisorPool requires a filesystem object store or an "
                "explicit materialized filesystem projection"
            )
        with self._lock:
            try:
                self._boot_locked()
            except Exception:
                for handle in self._handles:
                    if handle is not None:
                        self.provider.stop_serve(handle)
                self._handles = []
                self._stop_egress_locked()
                raise

    def _boot_locked(self) -> None:
        """Make the bound dirs + boot the warm sandbox(es); caller holds ``_lock``."""
        if getattr(self, "_retired", False):
            raise RuntimeError("Task sandbox pool was permanently retired.")
        started = time.perf_counter()
        os.makedirs(self._runs_root, exist_ok=True)  # B1
        os.makedirs(self.work_root, exist_ok=True)
        for slot in range(self._slot_count):
            self._clear_slot_control_files(slot)
            os.makedirs(self._slot_inbox(slot), exist_ok=True)
            os.makedirs(self._slot_outbox(slot), exist_ok=True)

        ro_binds = self._runtime_ro_binds()
        env = _workflow_python_env()
        self._ensure_egress_locked()
        env.update(self._egress_proxy_env)
        if self.fileops:
            env.update(_AGENT_SHELL_ENV)
        command = [
            sys.executable,
            "-m",
            "vibecanvas_api.sandbox_entry",
            "serve-parallel",
            "/work",
            "/runs",
            str(self.size),
        ]
        # Explicit kwarg WINS over config.sandbox_network in run_serve's
        # _resolve_network (default None → config default). Fileops intentionally
        # leave this unset too: the agent shell should follow SANDBOX_NETWORK
        # (dev default: host networking) instead of silently blocking curl/wget.
        network: "str | None" = (
            "none" if self._egress_socket is not None else None
        )
        if self.fileops:
            logger.warning(
                "agent_sandbox_warm_pool_boot_start",
                fileops=True,
                size=self.size,
                runs_root=self._runs_root,
                work_root=self.work_root,
                bind_dests=[dest for dest, _src in self.fileop_binds or []],
            )
            # Task 4b-i — confine file ops to the agent's CLEAN mount dests (NOT
            # /runs). The serve loop reads VIBECANVAS_FILEOP_ROOTS (colon-sep
            # absolute dests); the binds are mounted by run_serve below. Unset →
            # Task 4a behavior (roots default to runs_root in the serve loop).
            roots = self.fileop_roots
            if roots is None and self.fileop_binds:
                roots = [d for d, _ in self.fileop_binds]
            if roots:
                env["VIBECANVAS_FILEOP_ROOTS"] = ":".join(
                    d for d in roots
                )
        self._handles = []
        for slot in range(self._slot_count):
            if not self.fileops:
                logger.warning(
                    "workflow_warm_pool_boot_start",
                    fileops=False,
                    slot=slot,
                    ro_bind_count=len(ro_binds),
                    ro_binds=ro_binds,
                    py_path_count=len(_workflow_python_paths()),
                )
            handle = self.provider.run_serve(
                runs_root=self._runs_root,
                work_dir=self._slot_work_root(slot),
                ro_binds=ro_binds,
                env=env,
                command=command,
                network=network,
                extra_rw_binds=self.fileop_binds,
                egress_socket=self._egress_socket,
            )
            # Catch immediate mount/import failures at boot time instead of
            # reporting the next user job as a mysterious "worker died".
            time.sleep(0.05)
            exit_code = handle.proc.poll()
            if isinstance(exit_code, int):
                details = self._dead_worker_details(handle)
                self.provider.stop_serve(handle)
                logger.warning(
                    "agent_sandbox_warm_pool_boot_failed",
                    fileops=self.fileops,
                    slot=slot,
                    details=details,
                )
                raise RuntimeError(f"warm sandbox worker failed to start: {details}")
            if self.fileops and isinstance(getattr(handle.proc, "pid", None), int):
                self._wait_for_fileop_ready(handle, slot=slot, started=started)
            elif not self.fileops:
                logger.warning(
                    "workflow_warm_pool_boot_done",
                    fileops=False,
                    slot=slot,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            self._handles.append(handle)
        if self.fileops:
            logger.warning(
                "agent_sandbox_warm_pool_boot_done",
                fileops=True,
                size=self.size,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

    def stop(self, *, retire: bool = False) -> None:
        """Stop the resident gVisor job server."""
        with self._lock:
            if retire:
                self._retired = True
            for handle in self._handles:
                if handle is not None:
                    self.provider.stop_serve(handle)
            for slot in range(self._slot_count):
                self._clear_slot_control_files(slot)
            self._handles = []
            self._stop_egress_locked()
            with self._activity_lock:
                self._abandoned_jobs.clear()

    def acquire_egress_hosts(
        self,
        hosts: set[str] | list[str] | tuple[str, ...],
    ) -> str | None:
        """Acquire an operation-scoped host lease on the resident broker."""
        if isinstance(hosts, str):
            hosts = (hosts,)
        normalized = {
            str(host).strip().lower()
            for host in hosts
            if str(host).strip()
        }
        if not normalized or self._egress_loop_thread is None:
            return None
        return self._egress_loop_thread.acquire_allow_hosts(normalized)

    def release_egress_hosts(self, lease_id: str | None) -> None:
        """Release an operation-scoped host lease; teardown is idempotent."""
        if lease_id is None or self._egress_loop_thread is None:
            return
        self._egress_loop_thread.release_allow_hosts(lease_id)

    def _ensure_egress_locked(self) -> None:
        if config.sandbox_egress_mode != "proxy" or self._egress_loop_thread is not None:
            return
        setup = self.provider._sandbox_egress_setup(
            f"resident:{self.tenant or 'shared'}:{self.work_root}",
            set(),
        )
        if setup is None:  # pragma: no cover - proxy mode is checked above
            raise RuntimeError("resident sandbox egress broker was not configured")
        loop_thread, socket_path, proxy_env = setup
        self._egress_loop_thread = loop_thread
        self._egress_socket = socket_path
        self._egress_proxy_env = proxy_env

    def _stop_egress_locked(self) -> None:
        loop_thread = self._egress_loop_thread
        self._egress_loop_thread = None
        self._egress_socket = None
        self._egress_proxy_env = {}
        if loop_thread is not None:
            loop_thread.stop()

    def checkpoint(
        self,
        *,
        image_dir: str,
        fingerprint: str,
        kind: str = "session_hibernation",
    ) -> ServeSnapshot:
        """Hibernate the single idle worker into a rootful gVisor snapshot.

        The caller owns quiescence: ``SandboxSession`` invokes this only after
        its activity counter reaches zero and VFS writeback completes. A failed
        checkpoint is surfaced; this method never silently cold-boots instead.
        """
        with self._lock:
            if len(self._handles) != 1 or self._handles[0] is None:
                raise RuntimeError("warm sandbox worker is not checkpointable")
            handle = self._handles[0]
            self.provider.checkpoint_serve(handle, image_dir=image_dir)
            # Checkpoint stops the container by default. Delete its runsc state
            # and bundle while retaining the caller-owned image directory.
            self.provider.stop_serve(handle)
            self._handles = []
        return ServeSnapshot(
            image_dir=image_dir,
            fingerprint=fingerprint,
            kind=kind,
        )

    def restore(self, snapshot: ServeSnapshot) -> None:
        """Restore a checkpointed worker and verify its actual job channel."""
        with self._lock:
            if self._handles:
                raise RuntimeError("cannot restore over a running sandbox worker")
            os.makedirs(self._runs_root, exist_ok=True)
            os.makedirs(self.work_root, exist_ok=True)
            for slot in range(self._slot_count):
                os.makedirs(self._slot_inbox(slot), exist_ok=True)
                os.makedirs(self._slot_outbox(slot), exist_ok=True)
            ro_binds = self._runtime_ro_binds()
            env = _workflow_python_env()
            self._ensure_egress_locked()
            env.update(self._egress_proxy_env)
            if self.fileops:
                env.update(_AGENT_SHELL_ENV)
                roots = self.fileop_roots
                if roots is None and self.fileop_binds:
                    roots = [destination for destination, _source in self.fileop_binds]
                if roots:
                    env["VIBECANVAS_FILEOP_ROOTS"] = ":".join(roots)
            command = [
                sys.executable,
                "-m",
                "vibecanvas_api.sandbox_entry",
                "serve-parallel",
                "/work",
                "/runs",
                str(self.size),
            ]
            handle = self.provider.restore_serve(
                snapshot=snapshot,
                runs_root=self._runs_root,
                work_dir=self.work_root,
                ro_binds=ro_binds,
                env=env,
                command=command,
                network="none" if self._egress_socket is not None else None,
                extra_rw_binds=self.fileop_binds,
                egress_socket=self._egress_socket,
            )
            self._handles = [handle]

        # A stale ready marker is insufficient proof after restore. Exercise the
        # real channel before user work is accepted. An operation-level error is
        # acceptable; receiving a shaped result proves the serve loop responded.
        time.sleep(0.05)
        if handle.proc.poll() is not None:
            details = self._dead_worker_details(handle)
            self.stop()
            raise RuntimeError(f"restored sandbox worker exited early: {details}")
        probe_root = (self.fileop_roots or ["/"])[0]
        probe = self.submit_sandbox_job(
            {"kind": "fileop", "op": {"op": "list", "path": probe_root}},
            timeout=float(config.sandbox_snapshot_restore_timeout_s),
        )
        if not isinstance(probe, dict) or "ok" not in probe:
            self.stop()
            raise RuntimeError("restored sandbox worker failed its channel probe")

    def _restart_slot_locked(self, slot: int) -> None:
        """Restart the resident worker and sweep its inbox markers.

        Caller holds ``_lock``. ``slot`` remains in the signature because the
        channel helpers are slot-indexed, but there is exactly one gVisor sandbox;
        concurrency lives inside the sandbox job server.
        """
        if slot < len(self._handles):
            if self._handles[slot] is not None:
                self.provider.stop_serve(self._handles[slot])
            self._handles[slot] = None
        try:
            for name in os.listdir(self._slot_inbox(slot)):
                if name.endswith((".taken", ".ready", ".json")):
                    try:
                        os.remove(os.path.join(self._slot_inbox(slot), name))
                    except OSError:
                        pass
        except OSError:
            pass
        self._handles[slot] = self._boot_one_locked(slot)

    def _boot_one_locked(self, slot: int):
        """Boot the resident worker. Caller holds ``_lock``."""
        started = time.perf_counter()
        self._clear_slot_control_files(slot)
        ro_binds = self._runtime_ro_binds()
        env = _workflow_python_env()
        self._ensure_egress_locked()
        env.update(self._egress_proxy_env)
        if self.fileops:
            env.update(_AGENT_SHELL_ENV)
        command = [
            sys.executable,
            "-m",
            "vibecanvas_api.sandbox_entry",
            "serve-parallel",
            "/work",
            "/runs",
            str(self.size),
        ]
        network: "str | None" = (
            "none" if self._egress_socket is not None else None
        )
        if self.fileops:
            roots = self.fileop_roots
            if roots is None and self.fileop_binds:
                roots = [d for d, _ in self.fileop_binds]
            if roots:
                env["VIBECANVAS_FILEOP_ROOTS"] = ":".join(
                    d for d in roots
                )
        if not self.fileops:
            logger.warning(
                "workflow_warm_pool_boot_start",
                fileops=False,
                slot=slot,
                ro_bind_count=len(ro_binds),
                ro_binds=ro_binds,
                py_path_count=len(_workflow_python_paths()),
            )
        handle = self.provider.run_serve(
            runs_root=self._runs_root,
            work_dir=self._slot_work_root(slot),
            ro_binds=ro_binds,
            env=env,
            command=command,
            network=network,
            extra_rw_binds=self.fileop_binds,
            egress_socket=self._egress_socket,
        )
        if self.fileops and isinstance(getattr(handle.proc, "pid", None), int):
            self._wait_for_fileop_ready(handle, slot=slot, started=started)
        elif not self.fileops:
            logger.warning(
                "workflow_warm_pool_boot_done",
                fileops=False,
                slot=slot,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        return handle

    @staticmethod
    def _runtime_ro_binds() -> list[str]:
        """Base Python runtime plus the shared public-package overlay cache.

        The cache root is mounted once when the warm sandbox starts. New
        content-addressed children published by the host become visible through
        that same read-only bind, so a Workflow can prepare dependencies before
        a job without rebuilding the resident sandbox or installing into it.
        """
        binds = list(_workflow_python_binds())
        overlay_root = os.path.abspath(config.lib_overlay_root)
        os.makedirs(overlay_root, exist_ok=True)
        if overlay_root not in binds:
            binds.append(overlay_root)
        return binds

    def _clear_slot_control_files(self, slot: int) -> None:
        """Remove stale lifecycle sentinels before booting the worker channel.

        ``stop()`` asks the serve loop to exit by writing ``shutdown`` under the
        slot work dir. The work dir is intentionally reused across remounts, so
        a later worker must clear that sentinel first; otherwise it starts,
        observes shutdown, exits cleanly, and the host reports a dead worker with
        ``exit_code=0`` on the next job.
        """
        slot_root = self._slot_work_root(slot)
        os.makedirs(slot_root, exist_ok=True)
        for name in ("ready", "shutdown"):
            try:
                os.remove(os.path.join(slot_root, name))
            except OSError:
                pass

    def activity_snapshot(self) -> dict[str, int | bool | None]:
        """Return content-free activity facts without submitting a guest job.

        `abandoned_jobs` is host-authoritative. The remaining fields are
        defense-in-depth observations from the credential-free /work channel.
        """
        with self._activity_lock:
            abandoned = list(self._abandoned_jobs)
        for job_id in abandoned:
            done = os.path.join(self._outbox, f"{job_id}.done")
            if os.path.exists(done):
                self._cleanup_job_markers(job_id)
                with self._activity_lock:
                    self._abandoned_jobs.discard(job_id)

        try:
            inbox_names = os.listdir(self._inbox)
            state_path = os.path.join(self.work_root, "activity.json")
            state: dict = {}
            try:
                descriptor = os.open(
                    state_path,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                        raise ValueError("invalid sandbox activity state file")
                    raw = os.read(descriptor, 4097)
                finally:
                    os.close(descriptor)
                if len(raw) > 4096:
                    raise ValueError("sandbox activity state file is too large")
                candidate = json.loads(raw.decode("utf-8"))
                if isinstance(candidate, dict):
                    state = candidate
            except (OSError, UnicodeError, ValueError):
                pass
            handle = self._handle_for_slot(0)
            alive = bool(handle is not None and handle.proc.poll() is None)
            with self._activity_lock:
                abandoned_count = len(self._abandoned_jobs)
            return {
                "alive": alive,
                "queued_jobs": sum(name.endswith(".ready") for name in inbox_names),
                "claimed_jobs": sum(name.endswith(".taken") for name in inbox_names),
                "active_markers": 0,
                "reported_active_jobs": max(0, int(state.get("active_jobs") or 0)),
                "activity_sequence": max(0, int(state.get("sequence") or 0)),
                "abandoned_jobs": abandoned_count,
            }
        except (OSError, TypeError, ValueError):
            return {
                "alive": False,
                "queued_jobs": 0,
                "claimed_jobs": 0,
                "active_markers": 0,
                "reported_active_jobs": 0,
                "activity_sequence": None,
                "abandoned_jobs": len(self._abandoned_jobs),
            }

    def is_quiescent(self, *, stable_polls: int = 2) -> bool:
        """Require consecutive idle samples before checkpointing the worker."""
        polls = max(2, int(stable_polls))
        for index in range(polls):
            state = self.activity_snapshot()
            if (
                not state["alive"]
                or state["activity_sequence"] is None
                or int(state["queued_jobs"] or 0) > 0
                or int(state["claimed_jobs"] or 0) > 0
                or int(state["active_markers"] or 0) > 0
                or int(state["reported_active_jobs"] or 0) > 0
                or int(state["abandoned_jobs"] or 0) > 0
            ):
                return False
            if index + 1 < polls:
                time.sleep(min(0.1, max(0.01, self.poll_interval)))
        return True

    def _wait_for_fileop_ready(self, handle, *, slot: int, started: float,
                               timeout: float = 120.0) -> None:
        ready_path = os.path.join(self._slot_work_root(slot), "ready")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(ready_path):
                logger.warning(
                    "agent_sandbox_warm_pool_ready",
                    fileops=True,
                    slot=slot,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                return
            exit_code = handle.proc.poll()
            if isinstance(exit_code, int):
                details = self._dead_worker_details(handle)
                logger.warning(
                    "agent_sandbox_warm_pool_boot_failed",
                    fileops=True,
                    slot=slot,
                    details=details,
                )
                raise RuntimeError(f"warm sandbox worker failed before ready: {details}")
            time.sleep(self.poll_interval)
        details = self._dead_worker_details(handle)
        raise RuntimeError(
            f"warm sandbox worker did not become ready after {timeout:.1f}s. {details}"
        )

    def _restart_worker(self) -> None:
        """Restart the warm sandbox(es). Kept for workflow warm paths and hard cancel."""
        for handle in self._handles:
            if handle is not None:
                self.provider.stop_serve(handle)
        self._handles = []
        with self._activity_lock:
            self._abandoned_jobs.clear()
        for slot in range(self._slot_count):
            try:
                for name in os.listdir(self._slot_inbox(slot)):
                    if name.endswith((".taken", ".ready", ".json")):
                        try:
                            os.remove(os.path.join(self._slot_inbox(slot), name))
                        except OSError:
                            pass
            except OSError:
                pass
        self._boot_locked()

    def _ensure_started(self) -> None:
        expected = self._slot_count
        if len(self._handles) != expected or any(h is None for h in self._handles):
            with self._lock:
                if len(self._handles) != expected or any(h is None for h in self._handles):
                    self._restart_worker() if self._handles else self._boot_locked()

    @staticmethod
    def _dead_worker_details(handle) -> str:
        """Return a short, non-blocking-ish stderr/stdout tail for a dead worker."""
        try:
            stdout, stderr = handle.proc.communicate(timeout=0.2)
        except Exception:
            stdout, stderr = "", ""
        pieces = []
        if stderr:
            pieces.append("stderr=" + stderr[-2000:].strip())
        if stdout:
            pieces.append("stdout=" + stdout[-1000:].strip())
        return "; ".join(pieces) or f"exit_code={handle.proc.poll()}"

    def _handle_for_slot(self, slot: int):
        if slot >= len(self._handles):
            return None
        return self._handles[slot]

    @property
    def _handle(self):  # pragma: no cover - compatibility for older tests/debug probes
        return self._handle_for_slot(0)

    @_handle.setter
    def _handle(self, value) -> None:  # pragma: no cover - compatibility shim
        if value is None:
            self._handles = []
            return
        if not self._handles:
            self._handles = [value]
        else:
            self._handles[0] = value

    # -- generic sandbox job slots ----------------------------------------
    def submit_sandbox_job(self, job: dict, *, timeout: float | None = 30.0) -> dict:
        """Enqueue ONE sandbox job.

        This is the reusable scheduling layer for agent-visible sandbox
        commands. In fileops mode there is one gVisor sandbox and one shared
        channel; the in-sandbox ``serve-parallel`` loop owns concurrent dispatch.
        The transport (inbox ``.json`` + atomic ``.ready``, outbox ``.done`` +
        ``.result.json``) is an IMPLEMENTATION DETAIL kept entirely inside this
        method: the return is a plain result dict and callers never see the
        channel. A future remote backend would reimplement ``submit_sandbox_job``
        over RPC behind the same signature.

        On any timeout/death the return is a result-SHAPED error dict
        ``{"ok": False, "error": ...}`` so callers handle infrastructure failures
        uniformly with real operation failures.
        """
        self._ensure_started()
        deadline = time.monotonic() + timeout if timeout is not None else None
        return self._submit_sandbox_job_on_slot(
            job, slot=0, deadline=deadline, timeout=timeout)

    def _submit_sandbox_job_on_slot(self, job: dict, *, slot: int, deadline: float | None,
                                    timeout: float | None) -> dict:
        inbox = self._slot_inbox(slot)
        outbox = self._slot_outbox(slot)

        submit_started = time.perf_counter()
        job_id = uuid4().hex
        op = job.get("op") if isinstance(job.get("op"), dict) else {}
        job_kind = job.get("kind")
        op_kind = op.get("op") if isinstance(op, dict) else None
        # Drop the inbox job descriptor + atomic .ready marker (write-then-rename
        # so the worker never reads a half-written marker), mirroring _prep_job's
        # channel writes but with a generic sandbox job descriptor.
        with open(
            os.path.join(inbox, f"{job_id}.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(job, f)
        ready = os.path.join(inbox, f"{job_id}.ready")
        with open(ready + ".tmp", "w", encoding="utf-8") as f:
            f.write("")
        os.rename(ready + ".tmp", ready)
        ready_at = time.perf_counter()
        if self.fileops and job_kind != "terminal":
            logger.warning(
                "agent_sandbox_job_queued",
                job_id=job_id,
                slot=slot,
                kind=job_kind,
                op=op_kind,
                enqueue_elapsed_ms=int((ready_at - submit_started) * 1000),
                queue=self._queue_snapshot(),
            )

        done_path = os.path.join(outbox, f"{job_id}.done")
        result_path = os.path.join(outbox, f"{job_id}.result.json")
        taken_path = os.path.join(inbox, f"{job_id}.taken")
        taken_at: float | None = None
        while True:
            if taken_at is None and os.path.exists(taken_path):
                taken_at = time.perf_counter()
                if self.fileops and job_kind != "terminal":
                    logger.warning(
                        "agent_sandbox_job_taken",
                        job_id=job_id,
                        slot=slot,
                        kind=job_kind,
                        op=op_kind,
                        queued_ms=int((taken_at - ready_at) * 1000),
                        total_elapsed_ms=int((taken_at - submit_started) * 1000),
                        queue=self._queue_snapshot(),
                    )
            if os.path.exists(done_path):
                # The worker writes result.json atomically BEFORE .done, so by the
                # time .done is visible the result is present alongside it. Read
                # it, then sweep the .done + result file + inbox markers.
                try:
                    with open(result_path, "r", encoding="utf-8") as f:
                        result = json.load(f)
                except (OSError, ValueError):
                    result = {"ok": False, "error": "no_result"}
                try:
                    os.remove(done_path)
                except OSError:
                    pass
                try:
                    os.remove(result_path)
                except OSError:
                    pass
                self._cleanup_job_markers(job_id, slot=slot)
                with self._activity_lock:
                    self._abandoned_jobs.discard(job_id)
                done_at = time.perf_counter()
                if self.fileops and (job_kind != "terminal" or not isinstance(result, dict) or result.get("ok") is False):
                    logger.warning(
                        "agent_sandbox_job_done",
                        job_id=job_id,
                        slot=slot,
                        kind=job_kind,
                        op=op_kind,
                        ok=bool(result.get("ok", result.get("status") == "success")) if isinstance(result, dict) else None,
                        queued_ms=(
                            int((taken_at - ready_at) * 1000)
                            if taken_at is not None else None
                        ),
                        running_ms=(
                            int((done_at - taken_at) * 1000)
                            if taken_at is not None else None
                        ),
                        total_elapsed_ms=int((done_at - submit_started) * 1000),
                        exec_elapsed_ms=(
                            result.get("exec_elapsed_ms")
                            if isinstance(result, dict) else None
                        ),
                    )
                return result

            # The worker DIED (crashed, not hung): restart so the next submit isn't
            # wedged on a dead handle, then report a result-shaped error.
            handle = self._handle_for_slot(slot)
            exit_code = handle.proc.poll() if handle is not None else None
            if handle is not None and isinstance(exit_code, int):
                details = self._dead_worker_details(handle)
                with self._lock:
                    handle = self._handle_for_slot(slot)
                    exit_code = handle.proc.poll() if handle is not None else None
                    if handle is not None and isinstance(exit_code, int):
                        details = self._dead_worker_details(handle)
                        logger.warning(
                            "agent_sandbox_warm_worker_died",
                            fileops=self.fileops,
                            slot=slot,
                            job_id=job_id,
                            details=details,
                        )
                        if self.fileops:
                            self._restart_worker()
                        else:
                            self._restart_slot_locked(slot)
                self._cleanup_job_markers(job_id, slot=slot)
                return {
                    "ok": False,
                    "error": (
                        f"warm worker died while serving sandbox job {job_id} "
                        f"(restarted; resubmit). {details}"
                    ),
                }

            if deadline is not None and time.monotonic() >= deadline:
                # B3/B4: a RUNNING job (poisoned worker → kill+restart) vs a still-
                # QUEUED job (leave the worker — another job may be running).
                taken = os.path.join(inbox, f"{job_id}.taken")
                running = os.path.exists(taken)
                if running:
                    if not self.fileops:
                        with self._lock:
                            self._restart_slot_locked(slot)
                        self._cleanup_job_markers(job_id, slot=slot)
                    else:
                        # The guest thread continues after the host-side wait
                        # expires. Preserve a host-owned activity lease until
                        # activity_snapshot observes its terminal marker.
                        with self._activity_lock:
                            self._abandoned_jobs.add(job_id)
                    return {
                        "ok": False,
                        "error": (
                            f"warm sandbox job {job_id} timed out after "
                            f"{timeout:.1f}s while RUNNING"
                            + (
                                f" on slot {slot} — worker killed + restarted "
                                "(poisoned/hung)"
                                if not self.fileops
                                else " — job abandoned; sandbox remains available"
                            )
                        ),
                    }
                # Still queued (B4) — give up on this job, do NOT kill the worker.
                self._cleanup_job_markers(job_id, slot=slot)
                return {
                    "ok": False,
                    "error": (
                        f"warm sandbox job {job_id} timed out after {timeout:.1f}s "
                        f"while QUEUED on slot {slot} (worker busy with another job; resubmit) "
                        f"{self._queue_snapshot()}"
                    ),
                }

            time.sleep(self.poll_interval)

    # -- submit_fileop -----------------------------------------------------
    def submit_fileop(self, op: dict, *, timeout: float = 30.0) -> dict:
        """Run one agent file/shell op through the generic sandbox job scheduler."""
        return self.submit_sandbox_job({"kind": "fileop", "op": op}, timeout=timeout)

    def _queue_snapshot(self) -> str:
        try:
            chunks = []
            for slot in range(self._slot_count):
                names = os.listdir(self._slot_inbox(slot))
                ready = sum(1 for n in names if n.endswith(".ready"))
                taken = sum(1 for n in names if n.endswith(".taken"))
                jsons = sum(1 for n in names if n.endswith(".json"))
                done = sum(
                    1 for n in os.listdir(self._slot_outbox(slot))
                    if n.endswith(".done")
                )
                chunks.append(
                    f"slot{slot}:ready={ready},taken={taken},json={jsons},done={done}"
                )
            return "[queue " + " ".join(chunks) + "]"
        except OSError:
            return "[queue unavailable]"

    # -- submit ------------------------------------------------------------
    def submit(
        self,
        *,
        workflow: dict,
        inputs: dict,
        run_id: str,
        tenant: str,
        run_subpath: str | None = None,
        run_dir: str | None = None,
        timeout: float = 60.0,
    ) -> EngineRunResult:
        """Enqueue ONE workflow run on the warm worker + wait for its result.

        Pure-engine guarded FIRST (B2). ``job_id`` is a fresh uuid (≠ run_id so a
        debug-reexec of the same run_id can't collide on the channel). The
        caller's ``timeout`` includes QUEUE WAIT (the worker serializes jobs).
        On a RUNNING-job timeout (``.taken`` present) the poisoned worker is
        killed+restarted (B3); on a QUEUED timeout (``.taken`` absent) the worker
        is left alone, only this job's markers cleaned (B4).
        """
        # Guard FIRST — classify BEFORE touching the channel / run-tier. Raises
        # Host/API nodes fail before touching the channel. They must use a
        # host-brokered platform path; this pool never receives a database role.
        self._classify_guard(workflow)
        job_id, exec_dir = self._prep_job(
            workflow=workflow, inputs=inputs, run_id=run_id, tenant=tenant,
            run_subpath=run_subpath, run_dir=run_dir,
        )

        done_path = os.path.join(self._outbox, f"{job_id}.done")
        deadline = time.monotonic() + timeout
        while True:
            if os.path.exists(done_path):
                try:
                    os.remove(done_path)
                except OSError:
                    pass
                return self.provider._read_engine_result(
                    exec_dir, self._sandbox_snapshot()
                )

            # The worker DIED (crashed, not hung): restart + report so the next
            # submit isn't wedged on a dead handle.
            handle = self._handle_for_slot(0)
            if handle is not None and handle.proc.poll() is not None:
                with self._lock:
                    handle = self._handle_for_slot(0)
                    if handle is not None and handle.proc.poll() is not None:
                        self._restart_worker()
                self._cleanup_job_markers(job_id)
                return self._error_result(
                    f"warm worker died while serving job {job_id} "
                    "(restarted; resubmit the run)"
                )

            if time.monotonic() >= deadline:
                # B3/B4: distinguish a RUNNING job (poisoned worker → kill+
                # restart) from a still-QUEUED job (leave the worker — another
                # job may legitimately be running).
                taken = os.path.join(self._inbox, f"{job_id}.taken")
                running = os.path.exists(taken)
                if running:
                    with self._lock:
                        self._restart_worker()  # B3
                    self._cleanup_job_markers(job_id)
                    return self._error_result(
                        f"warm job {job_id} timed out after {timeout:.1f}s while "
                        "RUNNING — worker killed + restarted (poisoned/hung)"
                    )
                # Still queued (B4) — give up on this job, do NOT kill the worker.
                self._cleanup_job_markers(job_id)
                return self._error_result(
                    f"warm job {job_id} timed out after {timeout:.1f}s while "
                    "QUEUED (worker busy with another job; resubmit)"
                )

            time.sleep(self.poll_interval)

    # -- submit_stream (live tail) -----------------------------------------
    async def submit_stream(
        self,
        *,
        workflow: dict,
        inputs: dict,
        run_id: str,
        tenant: str,
        run_subpath: str | None = None,
        run_dir: str | None = None,
        timeout: float | None = 120.0,
    ) -> AsyncIterator[dict]:
        """STREAMING sibling of :meth:`submit` (RE-6 debug-execute). Enqueue one
        run, then TAIL the run's ``__exec__/events.ndjson`` (the in-sandbox engine
        flushes per line; the bind-mount makes appended lines host-visible live)
        and yield, in order:

          * ``{"type": "node_event", **raw_event}`` per NEW non-terminal astream
            event (the engine ``finished`` event is SKIPPED here so the terminal
            ``result`` is the SOLE terminal frame — the route maps that to its
            ``completed`` frame; double-emit would race the SSE fence).
          * ``{"type": "result", final_outputs, error_dict, execution_time}`` once
            the outbox ``.done`` marker appears (read from ``result.json``), then
            STOP.

        Availability mirrors blocking ``submit`` (B3/B4): if neither ``.done`` nor
        ANY event progress is seen within ``timeout`` while the job is RUNNING
        (``.taken`` present) the worker is killed+restarted and a terminal
        ``{"type": "timeout"}`` is yielded; a still-QUEUED timeout leaves the
        worker alone and yields the same terminal. The pool never wedges.

        The guard runs SYNCHRONOUSLY before the first yield (so a caller can fall
        back in-process pre-stream on a host-only/non-runnable wf — the spec's
        PRE-YIELD-ONLY fallback). An EXPLICIT cancel = :meth:`cancel` (HARD: marker
        + kill+restart the worker, #483) so a mid-node runaway is interrupted
        promptly; this generator's own teardown (consumer-abandon / normal
        completion without a result) writes only the GRACEFUL
        :meth:`_write_cancel_marker` (no reboot — see the ``finally`` below).
        """
        # PRE-YIELD guard (B2/P2). Raising here (before the generator first
        # suspends) lets the manager/route fall back in-process. NOTE: in an async
        # generator the raise surfaces on the FIRST ``__anext__``, which is exactly
        # when the route starts the ``async for`` — still before any frame.
        self._classify_guard(workflow)
        job_id, exec_dir = self._prep_job(
            workflow=workflow, inputs=inputs, run_id=run_id, tenant=tenant,
            run_subpath=run_subpath, run_dir=run_dir,
            execution_timeout=timeout,
        )

        events_path = os.path.join(exec_dir, "events.ndjson")
        done_path = os.path.join(self._outbox, f"{job_id}.done")
        offset = 0            # bytes consumed from events.ndjson
        partial = ""          # buffered trailing partial line (no newline yet)
        last_progress = time.monotonic()  # last time .done OR a new event seen
        emitted_result = False

        try:
            while True:
                done = os.path.exists(done_path)

                # Read appended bytes (file may not exist until node 1 runs).
                new_lines, offset, partial = self._read_appended_lines(
                    events_path, offset, partial
                )
                for raw in new_lines:
                    last_progress = time.monotonic()
                    ev = self._parse_event_line(raw)
                    if ev is None:
                        continue
                    # SKIP the terminal engine event — ``result`` (below) is the
                    # sole terminal frame. ``finished`` carries final_outputs which
                    # result.json already holds authoritatively.
                    if ev.get("status") == "finished":
                        continue
                    yield {"type": "node_event", **ev}

                if done:
                    # Drain any final buffered complete lines once more (the engine
                    # writes .done AFTER result.json; events.ndjson is already
                    # fully flushed by then), then read the authoritative result.
                    final_lines, offset, partial = self._read_appended_lines(
                        events_path, offset, partial
                    )
                    for raw in final_lines:
                        ev = self._parse_event_line(raw)
                        if ev is None or ev.get("status") == "finished":
                            continue
                        yield {"type": "node_event", **ev}
                    try:
                        os.remove(done_path)
                    except OSError:
                        pass
                    res = self.provider._read_engine_result(
                        exec_dir, self._sandbox_snapshot()
                    )
                    emitted_result = True
                    yield {
                        "type": "result",
                        "final_outputs": res.final_outputs,
                        "error_dict": res.error_dict,
                        "execution_time": res.execution_time,
                    }
                    return

                # Worker DIED (crashed): restart so the pool isn't wedged, surface
                # a terminal error frame.
                handle = self._handle_for_slot(0)
                if handle is not None and handle.proc.poll() is not None:
                    with self._lock:
                        handle = self._handle_for_slot(0)
                        if handle is not None and handle.proc.poll() is not None:
                            self._restart_worker()
                    self._cleanup_job_markers(job_id)
                    yield {
                        "type": "timeout",
                        "message": (
                            f"warm worker died while serving job {job_id} "
                            "(restarted; resubmit the run)"
                        ),
                    }
                    return

                # B3/B4 hang: no .done + no progress within ``timeout``.
                if timeout is not None and time.monotonic() - last_progress >= timeout:
                    taken = os.path.join(self._inbox, f"{job_id}.taken")
                    running = os.path.exists(taken)
                    if running:
                        with self._lock:
                            self._restart_worker()  # B3 — poisoned worker
                        self._cleanup_job_markers(job_id)
                        yield {
                            "type": "timeout",
                            "message": (
                                f"warm job {job_id} timed out after "
                                f"{timeout:.1f}s while RUNNING — worker killed "
                                "+ restarted (poisoned/hung)"
                            ),
                        }
                    else:
                        # B4 — still queued; leave the worker, drop this job.
                        self._cleanup_job_markers(job_id)
                        yield {
                            "type": "timeout",
                            "message": (
                                f"warm job {job_id} timed out after "
                                f"{timeout:.1f}s while QUEUED (worker busy; "
                                "resubmit)"
                            ),
                        }
                    return

                await asyncio.sleep(self.poll_interval)
        finally:
            # Consumer-abandon (GeneratorExit) / normal completion: best-effort
            # write the GRACEFUL ``cancel`` marker if the run never produced a
            # result, so an orphaned run is nudged to stop at the next node
            # boundary and doesn't keep the worker busy. NOTE this is MARKER-ONLY
            # — NOT the hard kill+restart of :meth:`cancel`:
            #   * the EXPLICIT hard cancel arrives via the backend → manager →
            #     :meth:`cancel` path (which kills+restarts promptly, the #483
            #     fix for a mid-node runaway); this finally must not redundantly
            #     reboot the worker AGAIN on the GeneratorExit that the explicit
            #     cancel's aclose() cascade throws in here, nor punish a plain
            #     client-disconnect (no explicit cancel) with a ~20s cold boot.
            #   * the hang/death paths above ALREADY restarted the worker; do not
            #     restart a second time.
            if not emitted_result:
                self._write_cancel_marker(
                    run_id=run_id, tenant=tenant, run_subpath=run_subpath,
                )

    def cancel(
        self, *, run_id: str, tenant: str, run_subpath: str | None = None
    ) -> None:
        """HARD cancel (task #483 part 1): write the run's ``__exec__/cancel``
        marker AND kill+restart the per-tenant warm worker.

        Two mechanisms, belt-and-suspenders:

        * **marker** — the in-sandbox entrypoint's watcher picks it up and sets
          the astream ``stop_event`` → if the worker happens to be AT a node
          boundary it stops GRACEFULLY (the current node finishes; no new node
          starts), writes a partial ``result.json`` + ``.done``.
        * **kill+restart** (:meth:`_restart_worker`, the exact mechanism the
          hang-recovery path uses) — the marker watcher only fires at node
          boundaries, so a MID-NODE runaway (a CodeNode in a busy/infinite loop)
          can NOT be interrupted by the marker alone: the single shared worker
          stays stuck until the node finishes. The kill terminates the runaway
          node IMMEDIATELY and frees the worker; ``_restart_worker`` then sweeps
          the inbox ``.taken``/``.ready``/``.json`` orphans + reboots a fresh
          worker so the NEXT submit on this tenant is not confused by stale
          channel state.

        The run_dir is RETAINED — cancel only ever writes the marker + reboots
        the worker; it never deletes the run-tier ``{run_id}`` dir (the route's
        RunWorkspace keeps ``retain=True`` for debug, so the killed run's
        partial state stays inspectable).

        COLLATERAL + COLD-BOOT TRADEOFF (documented so it is not a surprise):
        workflow hard-cancel restarts the whole warm pool today, so it can kill
        other workflow jobs on that pool and the NEXT run re-pays worker boot.
        Agent file/shell jobs run in a separate fileops sandbox and do not use
        this workflow hard-cancel path.

        Idempotent + best-effort: the marker write swallows OSError, and the
        kill+restart is guarded by ``_lock`` (the same critical section
        hang-recovery uses) so a concurrent cancel + hang-restart can't
        double-boot.
        """
        self._write_cancel_marker(
            run_id=run_id, tenant=tenant, run_subpath=run_subpath,
        )
        # Kill+restart the (possibly mid-node-stuck) worker. Guarded by _lock and
        # idempotent: _restart_worker stop_serve's the current handle (a no-op if
        # already None) then boots a fresh one + sweeps the inbox so the next
        # submit isn't confused. Same mechanism + locking discipline as B3
        # hang-recovery.
        with self._lock:
            self._restart_worker()

    def close_job_pool(self, *, pool_id: str, timeout: float = 15.0) -> dict:
        """Stop one execution-owned pool, retaining the sandbox and other pools."""
        if len(pool_id) != 32 or any(c not in "0123456789abcdef" for c in pool_id):
            raise ValueError("invalid execution pool ID")
        directory = os.path.join(self._slot_work_root(0), "pool-control")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, pool_id + ".stop"), "w"):
            pass
        done = os.path.join(directory, pool_id + ".done")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with open(done, encoding="utf-8") as stream:
                    return json.load(stream)
            except FileNotFoundError:
                time.sleep(0.02)
        raise TimeoutError("Execution pool shutdown could not be confirmed.")

    def kill_job(self, *, run_id: str, tenant: str, run_subpath: str) -> None:
        """Request hard cancellation of one job, without restarting this pool.

        Only the supervisor consumes worker.kill and kills that job's process
        group. The workflow engine receives no cooperative cancellation signal.
        A marker arriving before enqueue also prevents the job from starting.
        The caller must await the job's terminal response to confirm exit.
        """
        if self.tenant is not None and tenant != self.tenant:
            raise ValueError("job tenant does not match sandbox scope")
        if (not run_subpath or os.path.isabs(run_subpath) or "\\" in run_subpath
                or "\x00" in run_subpath or any(part in {"", ".", ".."} for part in run_subpath.split("/"))):
            raise ValueError("invalid job subpath")
        exec_dir = os.path.join(self._runs_root, run_subpath, "__exec__")
        os.makedirs(exec_dir, exist_ok=True)
        # Do not swallow I/O errors and falsely acknowledge cancellation.
        with open(os.path.join(exec_dir, "worker.kill"), "w"):
            pass

    def _write_cancel_marker(
        self, *, run_id: str, tenant: str, run_subpath: str | None = None
    ) -> None:
        """Write the run's ``__exec__/cancel`` marker (GRACEFUL node-boundary
        stop, the warm worker SURVIVES). The in-sandbox watcher sets the astream
        ``stop_event`` so the run stops at the next node boundary + writes a
        partial ``result.json`` + ``.done``. Idempotent + best-effort (a marker
        written after the run finished is ignored by the watcher). Used directly
        by ``submit_stream``'s orphan-cleanup finally (NO worker reboot); folded
        into the hard :meth:`cancel` (marker + kill+restart)."""
        if run_subpath:
            run_dir = os.path.join(self._runs_root, run_subpath)
        elif self.tenant is not None:
            run_dir = os.path.join(self._runs_root, run_id)
        else:
            run_dir = os.path.join(self._runs_root, tenant, run_id)
        exec_dir = os.path.join(run_dir, "__exec__")
        try:
            os.makedirs(exec_dir, exist_ok=True)
            open(os.path.join(exec_dir, "cancel"), "w").close()
        except OSError:
            pass

    # -- helpers -----------------------------------------------------------
    def _classify_guard(self, workflow: dict) -> None:
        """Credential-free admission shared by submit and submit_stream."""
        classify_workflow(workflow)

    def _prep_job(
        self,
        *,
        workflow: dict,
        inputs: dict,
        run_id: str,
        tenant: str,
        run_subpath: str | None = None,
        run_dir: str | None = None,
        execution_timeout: float | None = 600.0,
    ) -> "tuple[str, str]":
        """Write the run-tier ``__exec__/{workflow,inputs}.json`` + drop the inbox
        job descriptor + atomic ``.ready`` marker. Returns ``(job_id, exec_dir)``.
        Shared by submit + submit_stream (the channel protocol is identical)."""
        job_id = uuid4().hex
        # Compute the HOST run dir + the ``run_subpath`` the worker joins onto its
        # bound runs-root (T1). Per-tenant: the mount IS the tenant dir, so the run
        # is a direct child (``{runs_root}/{run_id}``) and the worker's subpath is
        # just ``run_id`` (NO tenant prefix). Shared (legacy): keep the
        # ``{runs_root}/{tenant}/{run_id}`` layout byte-identical so the existing
        # shared-pool tests stay green; the worker's subpath is ``{tenant}/{run_id}``.
        if run_subpath:
            effective_run_subpath = run_subpath.strip("/")
            effective_run_dir = run_dir or os.path.join(
                self._runs_root, effective_run_subpath)
        elif self.tenant is not None:
            effective_run_dir = os.path.join(self._runs_root, run_id)
            effective_run_subpath = run_id
        else:
            effective_run_dir = os.path.join(self._runs_root, tenant, run_id)
            effective_run_subpath = f"{tenant}/{run_id}"
        exec_dir = os.path.join(effective_run_dir, "__exec__")
        os.makedirs(exec_dir, exist_ok=True)
        with open(os.path.join(exec_dir, "workflow.json"), "w", encoding="utf-8") as f:
            json.dump(workflow, f, ensure_ascii=False)
        with open(os.path.join(exec_dir, "inputs.json"), "w", encoding="utf-8") as f:
            json.dump(inputs, f, ensure_ascii=False)

        # Job descriptor + atomic .ready (write-then-rename so the worker never
        # reads a half-written marker; the run-tier __exec__ writes above are
        # non-atomic, so the worker must only touch them AFTER seeing .ready).
        with open(os.path.join(self._inbox, f"{job_id}.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "kind": "workflow",
                    "execution_timeout": execution_timeout,
                    "tenant": tenant,
                    "run_id": run_id,
                    "run_subpath": effective_run_subpath,
                },
                f,
            )
        ready = os.path.join(self._inbox, f"{job_id}.ready")
        with open(ready + ".tmp", "w", encoding="utf-8") as f:
            f.write("")
        os.rename(ready + ".tmp", ready)
        return job_id, exec_dir

    @staticmethod
    def _read_appended_lines(
        events_path: str, offset: int, partial: str
    ) -> "tuple[list[str], int, str]":
        """Read bytes appended to ``events_path`` past ``offset``; return
        (complete_lines, new_offset, trailing_partial). Parses only up to the LAST
        newline; a trailing partial line (no ``\\n`` yet — the writer hasn't
        flushed it whole) is BUFFERED into the next poll so we never parse half a
        JSON object. Missing file (node 1 hasn't run) → no lines."""
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                f.seek(offset)
                chunk = f.read()
                new_offset = f.tell()
        except OSError:
            return [], offset, partial
        if not chunk:
            return [], offset, partial
        buf = partial + chunk
        if "\n" not in buf:
            # No complete line yet — keep buffering.
            return [], new_offset, buf
        *lines, partial = buf.split("\n")
        complete = [ln for ln in lines if ln.strip()]
        return complete, new_offset, partial

    @staticmethod
    def _parse_event_line(line: str) -> "dict | None":
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            return None
        return ev if isinstance(ev, dict) else None

    def _cleanup_job_markers(self, job_id: str, *, slot: int = 0) -> None:
        """Best-effort remove a job's inbox/outbox markers (after a timeout the
        worker may STILL eventually emit them — clear so a later poll/sweep won't
        mistake a stale marker for a fresh job)."""
        inbox = self._slot_inbox(slot)
        outbox = self._slot_outbox(slot)
        for p in (
            os.path.join(inbox, f"{job_id}.json"),
            os.path.join(inbox, f"{job_id}.ready"),
            os.path.join(inbox, f"{job_id}.taken"),
            os.path.join(outbox, f"{job_id}.done"),
            os.path.join(outbox, f"{job_id}.result.json"),
        ):
            try:
                os.remove(p)
            except OSError:
                pass

    @staticmethod
    def _sandbox_snapshot():
        """The warm worker has no per-job ``SandboxResult`` (it never exits);
        pass ``None`` so ``_read_engine_result`` reads the run-tier result.json
        directly. Kept a method for a future per-job stderr capture."""
        return None

    @staticmethod
    def _error_result(message: str) -> EngineRunResult:
        return EngineRunResult(
            final_outputs={},
            error_dict={"__engine__": message},
            execution_time=0.0,
            events=[],
            sandbox=None,
        )
