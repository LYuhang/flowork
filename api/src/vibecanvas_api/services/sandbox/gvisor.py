"""RootlessGvisorProvider — rootless gVisor (runsc) OS sandbox (RE-6 P1).

Builds a fresh OCI bundle (a busybox rootfs + a hand-authored ``config.json``)
and invokes ``runsc --rootless ... run`` to execute ONE command with the
run-tier directory bind-mounted at ``/run`` (the RE-1↔Tier-B FS seam).

The engine never imports this module; it belongs exclusively to the API-side
sandbox control plane.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import site
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import structlog

from .bus_broker import (
    IN_SANDBOX_BUS_DIR,
    IN_SANDBOX_BUS_SOCK,
    MAX_SOCKET_PATH,
)
from .egress_broker import EgressBroker
from .provider import SandboxResult

logger = structlog.get_logger(__name__)

# Environment variable the in-sandbox engine reads for the bus socket path. Kept
# in sync with ``vibecanvas_engine.sandbox_entry._BUS_SOCK_ENV`` (the engine owns
# the value; api sets it on the sandbox process env).
_BUS_SOCK_ENV = "VC_BUS_SOCK"

# Host system dirs bound read-only into the fresh rootfs so the busybox/runtime
# can find shared libs + config. Only those that actually exist are bound.
_HOST_RO_BINDS = ["/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc"]

# WSL commonly exposes ``/etc/resolv.conf`` as a symlink into ``/mnt/wsl``;
# systemd-resolved installations may point it into ``/run``. Binding ``/etc``
# alone leaves either symlink dangling inside the otherwise empty rootfs. Add
# the resolved file only when it lives outside an already mounted system root,
# preserving the small mount set on ordinary Linux hosts.
_RESOLVER_CONFIG = "/etc/resolv.conf"

# Default PATH injected into the sandboxed process env so bare commands (e.g.
# ``cat``, ``sh``) resolve against the read-only-bound host bin dirs. Without a
# PATH gVisor's loader gets an empty search path and fails with
# ``error finding executable "cat" in PATH []``. A caller-supplied ``PATH`` in
# ``env`` overrides this (it is only used as a fallback).
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Plan-B egress (B5) — the prod "proxy" path. In proxy mode the sandbox runs with
# ``--network=none`` and the in-sandbox forward proxy (started by the entrypoints)
# tunnels every outbound HTTP(S) over a bind-mounted host UNIX socket to a per-run
# host EgressBroker (allowlist relay). It mirrors the per-run workflow bus:
#   * host socket:        ``/tmp/vcegress/{run8}/egress.sock`` (≤107 bytes)
#   * in-sandbox mount:   ``/vcegress`` (rw bind; NOT under /run so it can't
#                         shadow the file channel)
#   * in-sandbox proxy listens on 127.0.0.1:_EGRESS_PROXY_PORT, reads VC_EGRESS_*.
EGRESS_ROOT = "/tmp/vcegress"
IN_SANDBOX_EGRESS_DIR = "/vcegress"
IN_SANDBOX_EGRESS_SOCK = IN_SANDBOX_EGRESS_DIR + "/egress.sock"

# The content-addressed Workflow dependency overlay, bound read-only at a path
# distinct from the agent's writable ``/opt/agent-overlay``. CodeNode places
# this custom layer before the explicitly selected platform base packages;
# both are appended after the Python standard library.
IN_SANDBOX_LIB_OVERLAY = "/opt/lib-overlay"
_LIB_OVERLAY_ENV = "VC_LIB_OVERLAY"
# The FIXED localhost port the in-sandbox forward proxy listens on; HTTP(S)_PROXY
# point the sandbox HTTP stack here. A module constant so the env + proxy agree.
_EGRESS_PROXY_PORT = 13128
# Env vars the in-sandbox proxy reads (the bind dest socket + its listen port).
_EGRESS_SOCK_ENV = "VC_EGRESS_SOCK"
_EGRESS_PORT_ENV = "VC_EGRESS_PORT"


def _egress_socket_path_for(run_id: str) -> str:
    """SHORT per-run host egress socket path (mirror of ``bus_broker.socket_
    path_for``): ``/tmp/vcegress/{run8}/egress.sock``, ASSERTED ≤107 bytes (the
    AF_UNIX pathname limit). Per-run dir, not a shared one, so one run's sandbox
    can't ``connect()`` a peer run's broker under ``--host-uds=open``."""
    # Prefix-based shortening made every ``agent-runtime-*`` collide at
    # ``agentrun``. A short digest preserves the AF_UNIX bound while remaining
    # unique across concurrently resident Chats and background jobs.
    run8 = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(EGRESS_ROOT, run8, "egress.sock")
    encoded = path.encode("utf-8")
    assert len(encoded) <= MAX_SOCKET_PATH, (
        f"egress socket path {path!r} is {len(encoded)} bytes > "
        f"{MAX_SOCKET_PATH} (AF_UNIX limit)"
    )
    return path


class _BrokerLoopThread:
    """Run an :class:`EgressBroker` on a DEDICATED background event loop thread
    for the DURATION of a (blocking, sync) sandbox run.

    The async-from-sync bridge for the egress broker. The bus broker is started
    on the route's LIVE loop (``await broker.start()``) because its consumer
    (``BusBroker.messages()``) runs there; the egress broker has NO host-side
    consumer — it only needs its UDS server to keep SERVING while the synchronous
    ``run()`` blocks on ``runsc``. ``run_workflow`` itself runs inside
    ``asyncio.to_thread`` (see ``services/sandbox_run.py``) where there is NO
    running loop, so we cannot ``await`` here. Instead we spin our own loop on a
    daemon thread: ``start()`` creates the server (blocking-wait on the bridge),
    the loop ``run_forever`` services accepts/relays during the run, and
    ``stop()`` schedules ``aclose()`` + stops the loop. This is the standard
    "run an asyncio server from sync code" pattern (a private loop thread +
    ``run_coroutine_threadsafe``)."""

    def __init__(self, broker: EgressBroker):
        self._broker = broker
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="vc-egress-loop", daemon=True
        )

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> None:
        """Start the loop thread and bind+listen the broker (BLOCK until bound)."""
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._broker.start(), self._loop)
        fut.result()  # propagate a bind failure synchronously

    def stop(self) -> None:
        """aclose() the broker on the loop, then stop + join the loop thread.
        Best-effort + idempotent."""
        try:
            fut = asyncio.run_coroutine_threadsafe(self._broker.aclose(), self._loop)
            fut.result(timeout=10.0)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        self._thread.join(timeout=5.0)
        try:
            self._loop.close()
        except Exception:
            pass

    def acquire_allow_hosts(self, hosts: set[str]) -> str | None:
        """Acquire one operation-scoped resident egress lease."""
        return self._broker.acquire_allow_hosts(hosts)

    def release_allow_hosts(self, lease_id: str | None) -> None:
        """Release one operation-scoped resident egress lease."""
        self._broker.release_allow_hosts(lease_id)


def _resolve_network(network: "str | None") -> str:
    """Resolve the runsc ``--network=`` value.

    An explicit ``network=`` kwarg WINS; otherwise fall back to the configured
    ``config.sandbox_network`` (itself defaulting to ``"host"``). So with the
    flag unset every argv stays byte-identical to the pre-seam ``--network=host``
    hardcode — nothing regresses.

    Rootless development uses ``host``. The runsc netstack requires a separately
    validated rootful/server deployment profile."""
    if network is not None:
        return network
    return config.sandbox_network


def build_oci_config(
    *,
    command: list[str],
    env: dict | None,
    run_dir: str | None = None,
    rw_binds: "list[tuple[str, str]] | None" = None,
    extra_ro_binds: "list[str] | tuple[str, ...]" = (),
    ro_binds: "list[tuple[str, str]] | None" = None,
    rootless: bool = True,
) -> dict:
    """Build the OCI ``config.json`` dict for a fresh rootless-gVisor bundle.

    Writable binds are the bidirectional FS seam (B2). Two ways to express them:

    - ``run_dir`` — back-compat single ``/run`` bind (P1/P2 ``run``/
      ``run_workflow``). Equivalent to ``rw_binds=[("/run", run_dir)]``.
    - ``rw_binds`` — a general ``[(destination, source), ...]`` list (RE-6 Warm:
      the warm worker binds TWO writable dirs, ``/runs`` + ``/work``). Each source
      is bind-mounted ``rw`` at its destination; the host ``/run`` tmpfs is NOT a
      source (a fresh rootfs gives clean mount points). Exactly one of
      ``run_dir`` / ``rw_binds`` is provided.

    ``extra_ro_binds``: additional host paths bind-mounted read-only with
    ``destination == source`` (host-path identity). The sandbox Python helpers
    use this for the configured interpreter/package paths; callers may add other
    explicit read-only paths as needed. Each is deduped against the existing
    mounts and only mounted if it exists on the host.

    ``ro_binds`` accepts general ``[(destination, source), ...]`` read-only
    binds where the destination DIFFERS from the source (unlike ``extra_ro_binds``
    which is host-path identity). Used for the content-addressed lib overlay
    (``/opt/lib-overlay`` ← a host cache path). Each uses the SAME ``["rbind",
    "ro"]`` mechanism as the host-deps RO binds. Default ``None`` keeps the prior
    behavior byte-for-byte.
    """
    if rw_binds is None:
        rw_binds = [("/run", run_dir)] if run_dir is not None else []
    # ``cwd`` is the first writable bind's destination (``/run`` for back-compat;
    # ``/runs`` for the warm worker — its serve loop uses absolute paths anyway).
    cwd = rw_binds[0][0] if rw_binds else "/"
    env = dict(env or {})
    # Ensure a PATH so the loader can resolve bare commands (see _DEFAULT_PATH).
    env.setdefault("PATH", _DEFAULT_PATH)
    # Resident Agent Runtime stdout is part of the development timing/debug
    # trail.  A pipe-backed Python process is block-buffered by default, which
    # otherwise hides the exact phase at which a first Turn is waiting.
    env.setdefault("PYTHONUNBUFFERED", "1")
    rw_destinations = {dest for dest, _source in rw_binds}
    mounts: list[dict] = [
        {"destination": "/proc", "type": "proc", "source": "proc"},
    ]
    if "/tmp" not in rw_destinations:
        mounts.append({"destination": "/tmp", "type": "tmpfs", "source": "tmpfs"})
    for host_dir in _HOST_RO_BINDS:
        if os.path.exists(host_dir):
            mounts.append(
                {
                    "destination": host_dir,
                    "type": "bind",
                    "source": host_dir,
                    "options": ["rbind", "ro"],
                }
            )
    identity_roots = [
        str(m["source"])
        for m in mounts
        if m.get("type") == "bind" and m.get("source") == m.get("destination")
    ]
    resolver_target = os.path.realpath(_RESOLVER_CONFIG)
    if (
        os.path.isfile(resolver_target)
        and not any(
            resolver_target == root
            or os.path.commonpath((resolver_target, root)) == root
            for root in identity_roots
        )
    ):
        mounts.append(
            {
                "destination": resolver_target,
                "type": "bind",
                "source": resolver_target,
                "options": ["bind", "ro"],
            }
        )
        identity_roots.append(resolver_target)
    # Extra read-only binds (B1) — host-path identity, deduped, existing
    # ABSOLUTE dirs only (an OCI Mount.Destination MUST be absolute; a relative
    # ``sys.path`` entry like ``.`` would make runsc reject the whole bundle).
    for host_dir in extra_ro_binds:
        normalized = os.path.abspath(host_dir) if host_dir else ""
        if (
            normalized
            and os.path.isabs(host_dir)
            and os.path.isdir(normalized)
            and not any(
                normalized == root
                or os.path.commonpath((normalized, root)) == root
                for root in identity_roots
            )
        ):
            mounts.append(
                {
                    "destination": normalized,
                    "type": "bind",
                    "source": normalized,
                    "options": ["rbind", "ro"],
                }
            )
            identity_roots.append(normalized)
    # Explicit read-only binds where destination differs from source (for example
    # lib overlay at /opt/lib-overlay ← a host cache path). Same ["rbind","ro"]
    # mechanism as the host-deps RO binds above; the source must exist on the host.
    for dest, source in ro_binds or []:
        if source and os.path.isdir(source):
            mounts.append(
                {
                    "destination": dest,
                    "type": "bind",
                    "source": source,
                    "options": ["rbind", "ro"],
                }
            )
    # The validated bidirectional FS seam — OUR clean writable binds (B2).
    for dest, source in rw_binds:
        mounts.append(
            {
                "destination": dest,
                "type": "bind",
                "source": source,
                "options": ["rbind", "rw"],
            }
        )

    # One destination must have exactly one filesystem owner. Duplicate OCI
    # destinations are both slow (another gofer/directfs mount) and ambiguous;
    # reject them before runsc sees the bundle. The total cap is an operational
    # guard against first-turn regressions caused by dynamically expanded
    # interpreter, Skill or CLI paths.
    destinations: set[str] = set()
    for mount in mounts:
        destination = str(mount.get("destination") or "")
        if not destination.startswith("/"):
            raise ValueError("OCI mount destination must be absolute")
        if destination in destinations:
            raise ValueError(f"duplicate OCI mount destination: {destination}")
        destinations.add(destination)
    if len(mounts) > config.sandbox_max_mounts:
        raise RuntimeError(
            "sandbox mount limit exceeded: "
            f"{len(mounts)} > {config.sandbox_max_mounts}"
        )

    # Install parents before children, irrespective of read/write ownership.
    # Pure Chat owns a writable /tmp channel; mounting it after an explicitly
    # authorized /tmp/<runtime> read-only bind hides that runtime completely.
    # A stable depth ordering preserves peer order and each mount's permissions.
    mounts.sort(key=lambda mount: os.path.normpath(mount["destination"]).count("/"))

    linux: dict[str, Any] = {
        "namespaces": [
            {"type": "mount"},
            {"type": "pid"},
            {"type": "ipc"},
        ],
    }
    if rootless:
        # runsc --rootless establishes the user namespace. These mappings make
        # container uid/gid 0 resolve to the unprivileged caller.
        linux["uidMappings"] = [
            {"containerID": 0, "hostID": os.getuid(), "size": 1}
        ]
        linux["gidMappings"] = [
            {"containerID": 0, "hostID": os.getgid(), "size": 1}
        ]

    return {
        "ociVersion": "1.0.0",
        "process": {
            "args": list(command),
            "env": [f"{k}={v}" for k, v in env.items()],
            "cwd": cwd,
            "user": {"uid": 0, "gid": 0},
            "terminal": False,
        },
        "root": {"path": "rootfs", "readonly": True},
        "mounts": mounts,
        "linux": linux,
    }


@dataclass
class ServeHandle:
    """Handle to a long-lived warm worker (RE-6 Warm T2).

    Unlike :meth:`RootlessGvisorProvider.run` (which builds → runs →
    communicates → tears down in a ``finally``), the warm worker's lifecycle is
    INVERTED: ``run_serve`` returns this handle WITHOUT waiting, and the bundle +
    runsc state_root must OUTLIVE the call — teardown happens in ``stop_serve``
    (at pool.stop), not inline. ``proc`` is the live ``runsc run`` Popen,
    ``run_id`` the runsc container id used to ``runsc delete`` it."""

    proc: "subprocess.Popen"
    bundle_dir: str
    state_root: str
    run_id: str
    network: str | None = None


@dataclass(frozen=True)
class ServeSnapshot:
    """Immutable checkpoint metadata used to restore a serve worker."""

    image_dir: str
    fingerprint: str
    # ``baseline`` is a clean, reusable startup cache. Interactive Chat state
    # must opt into ``session_hibernation`` explicitly so the two classes can
    # never be confused during restore or retention cleanup.
    kind: str = "baseline"
    format_version: int = 1


@dataclass
class BusRunHandle:
    """Handle to a non-blocking sandbox workflow run.

    Returned by :meth:`RootlessGvisorProvider.launch_workflow_bus` WITHOUT
    blocking, so the route can consume the bus broker live while the sandbox
    runs. ``proc`` is the live ``runsc run`` Popen (its process GROUP is killed by
    :meth:`stop_run`); ``container_id`` is the runsc container id to delete;
    ``exec_dir`` is the run-tier ``__exec__`` dir holding the crash-durable
    result.json / events.ndjson fallback."""

    proc: "subprocess.Popen"
    bundle_dir: str
    state_root: str
    container_id: str
    exec_dir: str
    network: str | None = None
    # Plan-B egress (B6): the per-run host EgressBroker loop thread, when this run
    # launched in ``proxy`` mode. ``stop_run`` ``stop()``s it on teardown so the
    # broker + its per-run UDS dir are cleaned up. ``None`` in host-network mode
    # (the bus path's default) — nothing to tear down.
    egress_loop_thread: object | None = None


class EngineNeedsHostNode(Exception):
    """A workflow contains a node ``run_workflow`` cannot run in-sandbox (RE-6 P2
    B2): its ``node_type`` is not in the FROZEN ``ENGINE_PURE_NODE_TYPES`` (e.g.
    an api-defined ``KnowledgeSearchNode`` needing Postgres egress). Raised by
    the pure-engine guard BEFORE launch — so a non-pure workflow fails fast on
    the host, never silently broken inside (DB egress is P2-next)."""


@dataclass
class EngineRunResult:
    """Result of running a workflow INSIDE the sandbox (RE-6 P2 §3).

    Mirrors the engine's ``(final_outputs, error_dict, execution_time)`` tuple
    plus the raw ``astream`` ``events`` (from ``events.ndjson``) and the
    underlying ``sandbox`` :class:`SandboxResult` (stdout/stderr/exit/timing)."""

    final_outputs: dict
    error_dict: dict
    execution_time: float
    events: list = field(default_factory=list)
    sandbox: "SandboxResult | None" = None


def _assert_pure_engine(workflow: dict) -> None:
    """Guard (B2): every node's ``node_type`` must be in the FROZEN engine-pure
    snapshot, else :class:`EngineNeedsHostNode`. The frozen set is captured in
    ``engine/.../nodes/__init__.py`` BEFORE any api node can pollute the live
    ``node_registry`` — so this CANNOT use the (polluted) live registry."""
    from vibecanvas_engine.nodes import ENGINE_PURE_NODE_TYPES

    for node_id, node in workflow.items():
        if node_id == "__meta__":
            continue
        node_type = (node or {}).get("node_type")
        if node_type not in ENGINE_PURE_NODE_TYPES:
            raise EngineNeedsHostNode(node_type)


def _module_source_root(module_name: str) -> str | None:
    """Return the editable source root for a package, when available."""
    spec = importlib.util.find_spec(module_name)
    origin = getattr(spec, "origin", None)
    if not origin or origin in {"built-in", "frozen"}:
        return None
    pkg_dir = os.path.dirname(os.path.abspath(origin))
    root = os.path.dirname(pkg_dir)
    return root if os.path.isdir(root) else None


def _runtime_has_installed_module(module_name: str, roots: list[str]) -> bool:
    """Return whether a mounted runtime already contains the application.

    Test and developer processes may import from a repository-level
    ``PYTHONPATH`` even after the isolated Runtime cache has been populated.
    Prefer that installed copy inside the mounted interpreter so a host source
    tree never needs to enter the Sandbox solely because of import precedence.
    """
    python_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for root in roots:
        candidates = (
            os.path.join(root, module_name),
            os.path.join(root, "lib", python_dir, "site-packages", module_name),
            os.path.join(root, "Lib", "site-packages", module_name),
        )
        if any(os.path.isfile(os.path.join(path, "__init__.py")) for path in candidates):
            return True
    return False


def _workflow_python_paths() -> "list[str]":
    """Editable source-root PYTHONPATH entries for sandbox execution.

    Normal dev/prod startup installs ``api`` and ``engine`` into the Python env
    selected by ``VIBECANVAS_PYTHON``. In that case this returns empty and the
    sandbox imports from ``VC_SANDBOX_PYTHON_PATHS`` after stdlib initialization.
    Only editable runs need source roots on ``PYTHONPATH``.

    Explicit dependency paths do not change that rule. An editable installation
    can coexist with ``SANDBOX_PYTHON_PATHS``; its ``.pth`` file points outside
    the mounted interpreter prefix, so the referenced source root must still be
    mounted and placed on ``PYTHONPATH``. Detect the actual module locations
    instead of assuming that a configured dependency path implies a copied
    installation.
    """
    # Development launch can mirror the current application packages into the
    # local runtime prefix before starting services. In that mode the sandbox
    # must not put the shared-workspace editable roots back on PYTHONPATH,
    # otherwise thousands of first-import metadata reads still hit NFS.
    if os.environ.get("VIBECANVAS_SANDBOX_USE_INSTALLED_APP") == "1":
        return []

    paths: list[str] = []
    runtime_roots = [
        p for p in (sys.prefix, sys.base_prefix, *_workflow_python_dependency_paths())
        if p
    ]
    for module_name in ("vibecanvas_engine", "vibecanvas_api"):
        if _runtime_has_installed_module(module_name, runtime_roots):
            continue
        root = _module_source_root(module_name)
        if not root:
            continue
        if any(root == base or _path_is_within(root, base) for base in runtime_roots):
            # The package is installed in the mounted Python environment already.
            # No PYTHONPATH/source bind is needed.
            continue
        if root not in paths:
            paths.append(root)
    return paths


def _workflow_python_dependency_paths() -> "list[str]":
    """Host package paths mounted into the sandbox and appended to ``sys.path``.

    These paths are deliberately appended by the sandbox entrypoints after the
    stdlib, not placed in ``PYTHONPATH``. This keeps Linux/runtime resolution on
    the host defaults while letting deployments explicitly choose the Python
    package set through ``SANDBOX_PYTHON_PATHS``.
    """
    paths: list[str] = []
    candidate_paths: list[str] = []
    configured_paths: list[str] = []
    try:
        from vibecanvas_api.config import config as _config
        configured_paths = list(getattr(_config, "sandbox_python_paths", []) or [])
    except Exception:
        pass
    if configured_paths:
        candidate_paths.extend(configured_paths)
    else:
        # Auto-detection is a local-dev fallback only. In deployment, set
        # SANDBOX_PYTHON_PATHS explicitly so the mount set stays predictable.
        for key in ("purelib", "platlib"):
            value = sysconfig.get_paths().get(key)
            if value:
                candidate_paths.append(value)
        try:
            candidate_paths.extend(site.getsitepackages())
        except Exception:
            pass
        try:
            candidate_paths.append(site.getusersitepackages())
        except Exception:
            pass
    for path in candidate_paths:
        if path and os.path.isabs(path) and os.path.isdir(path) and path not in paths:
            paths.append(path)
    return paths


def _path_is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(parent)]) == os.path.abspath(parent)
    except ValueError:
        return False


def _append_bind_if_needed(binds: list[str], path: str) -> None:
    if not path or not os.path.isabs(path) or not os.path.isdir(path):
        return
    if any(path == existing or _path_is_within(path, existing) for existing in binds):
        return
    binds.append(path)


def _workflow_python_binds() -> "list[str]":
    """Read-only binds for sandbox Python execution.

    The Linux environment is still the host environment via the stable system
    binds (``/bin``, ``/usr``, ``/lib`` ...). This function only adds Python
    runtime/package paths: the selected interpreter prefix and explicitly
    configured dependency paths.
    """
    binds: list[str] = []
    # uv virtual environments commonly use an absolute two-hop executable
    # link: .venv/bin/python -> <uv-cache>/<major-alias>/bin/python, where the
    # major alias is itself a symlink to the pinned patch release. Mounting only
    # sys.prefix and the real sys.base_prefix leaves that intermediate path
    # absent inside gVisor, so the kernel cannot load the interpreter. Include
    # the narrow executable-link prefix; build_oci_config still deduplicates it
    # against stable system mounts such as /usr.
    executable = os.path.abspath(sys.executable)
    if os.path.islink(executable):
        link_target = os.readlink(executable)
        if not os.path.isabs(link_target):
            link_target = os.path.abspath(
                os.path.join(os.path.dirname(executable), link_target)
            )
        _append_bind_if_needed(
            binds,
            os.path.dirname(os.path.dirname(link_target)),
        )
    for d in (
        sys.prefix,
        sys.base_prefix,
        *_workflow_python_dependency_paths(),
        *_workflow_python_paths(),
    ):
        _append_bind_if_needed(binds, d)
    return binds


def _workflow_python_env() -> dict[str, str]:
    # Bare python/pip commands must select the same mounted interpreter as
    # the worker, not a host system Python with a different dependency set.
    # Use only the selected runtime and trusted system paths, not host PATH.
    env: dict[str, str] = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + _DEFAULT_PATH,
    }
    source_paths = _workflow_python_paths()
    dep_paths = _workflow_python_dependency_paths()
    # Only editable application source roots belong on PYTHONPATH: Python puts
    # those entries *ahead* of the standard library during interpreter startup.
    # Dependency/site-package roots are appended by ``sandbox_entry`` through
    # VC_SANDBOX_PYTHON_PATHS after stdlib initialization. Mixing them here let
    # a legacy third-party ``enum34`` package shadow Python 3.11's stdlib enum,
    # crashing ``re`` before the worker could start. The host PYTHONPATH is also
    # intentionally not inherited; it can contain relative, host-cwd-specific
    # entries and the editable roots above are already resolved absolutely.
    if source_paths:
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(source_paths))
    if dep_paths:
        env["VC_SANDBOX_PYTHON_PATHS"] = os.pathsep.join(dep_paths)
    tiktoken_cache_dir = os.path.join(sys.prefix, "share", "tiktoken")
    if os.path.isdir(tiktoken_cache_dir):
        # The runtime environment/snapshot pre-populates this directory. It is
        # mounted read-only with sys.prefix, eliminating the first-turn network
        # fetch for OpenAI token encodings inside a new sandbox.
        env["TIKTOKEN_CACHE_DIR"] = tiktoken_cache_dir
    env["VIBECANVAS_STORAGE_ROOT"] = "/tmp/vibecanvas-local-data"
    # Never inherit host service credentials into a generic sandbox. Database,
    # Redis, object-store, KMS, and model credentials are host-only.
    for key in (
        "OBJECT_STORE_PROVIDER",
        "OBJECT_STORE_FS_ROOT",
        "VIBECANVAS_VFS_UPLOAD_MAX_BYTES",
        "LIB_OVERLAY_ROOT",
        "PYTHONNOUSERSITE",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


# Import the admission guard only after ``EngineNeedsHostNode`` is defined to
# break the small guard/provider cycle. This guard has no database exception.
from vibecanvas_api.config import config  # noqa: E402

from .workflow_guard import classify_workflow  # noqa: E402


class RootlessGvisorProvider:
    """One-shot rootless-gVisor command runner (boot → run → capture → teardown).

    This runner is used for capability probes, not the workflow execution
    interface.
    """

    def __init__(self, runsc_path: str):
        self._runsc = runsc_path
        self._rootless = True

    def _runtime_flags(
        self, network: "str | None", *, host_uds: bool = False
    ) -> list[str]:
        if not self._rootless and os.geteuid() != 0:
            raise PermissionError(
                "rootful gVisor requires sandboxd to run as uid 0"
            )
        flags = [self._runsc]
        if self._rootless:
            flags.append("--rootless")
        flags.extend(
            [
                f"--network={_resolve_network(network)}",
                "--ignore-cgroups",
                f"--platform={config.sandbox_gvisor_platform}",
                "--directfs=false",
            ]
        )
        if host_uds:
            flags.append("--host-uds=open")
        return flags

    def _build_bundle(
        self,
        *,
        command: list[str],
        env: dict | None,
        rw_binds: "list[tuple[str, str]]",
        ro_binds: "list[str] | tuple[str, ...]" = (),
        ro_dest_binds: "list[tuple[str, str]] | None" = None,
    ) -> "tuple[str, str, str]":
        """Build a fresh rootless-gVisor OCI bundle (a busybox-style rootfs with
        the mount-point dirs + a hand-authored ``config.json``) in a HOST temp
        dir. Returns ``(bundle_dir, state_root, run_id)``.

        Shared by ``run`` (one-shot, ``rw_binds=[("/run", run_dir)]``) and
        ``run_serve`` (long-lived, ``rw_binds=[("/runs", ...), ("/work", ...)]``).
        A mount-point dir is created in the rootfs for EACH bind destination so
        the bind/proc/tmpfs mounts have somewhere to land (the fresh rootfs is
        otherwise empty)."""
        # Bundle lives in a HOST temp dir, NOT under a tenant-visible run-tier — N3.
        bundle = tempfile.mkdtemp(prefix="vc-sbx-")
        run_id = uuid.uuid4().hex
        # Per-invocation runsc state dir (container metadata, control sockets).
        # The default (/var/run/runsc) is NOT writable by a rootless user, so we
        # point --root at a writable dir inside the bundle. Torn down with it.
        state_root = os.path.join(bundle, "state")
        os.makedirs(state_root, exist_ok=True)
        cfg = build_oci_config(
            command=command, env=env, rw_binds=rw_binds, extra_ro_binds=ro_binds,
            ro_binds=ro_dest_binds,
            rootless=self._rootless,
        )

        # Empty mount-point dirs in the fresh rootfs (busybox --install won't
        # make these; the bind/proc/tmpfs mounts need destinations to exist).
        rootfs = os.path.join(bundle, "rootfs")
        mount_dests = ["proc", "tmp"]
        for m in cfg["mounts"]:
            dest = m["destination"].lstrip("/")
            if dest and dest not in mount_dests:
                mount_dests.append(dest)
        for dest in mount_dests:
            target = os.path.join(rootfs, dest)
            matching_mount = next(
                (
                    mount
                    for mount in cfg["mounts"]
                    if mount["destination"].lstrip("/") == dest
                ),
                None,
            )
            source = str(matching_mount.get("source") or "") if matching_mount else ""
            if matching_mount and matching_mount.get("type") == "bind" and os.path.isfile(source):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "ab"):
                    pass
            else:
                os.makedirs(target, exist_ok=True)

        with open(os.path.join(bundle, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

        return bundle, state_root, run_id

    def run(
        self,
        *,
        run_dir: str,
        command: list[str],
        env: dict | None = None,
        network: "str | None" = None,
        timeout: float = 60.0,
        extra_ro_binds: "list[str] | tuple[str, ...]" = (),
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        bus_socket: str | None = None,
        egress_socket: str | None = None,
        lib_overlay: str | None = None,
        run_mount: str = "/run",
    ) -> SandboxResult:
        # The run channel is first so ``cwd`` stays there (build_oci_config uses
        # ``rw_binds[0][0]``). Workflow runs keep the public ``/run`` contract;
        # pure Chat code jobs use a narrow internal channel at ``/tmp`` so they do
        # not expose duplicate workspace paths under /run.
        rw_binds = [(run_mount, run_dir)]
        # Add writable binds such as the per-workflow agent overlay at
        # ``/opt/agent-overlay`` for the install path). Appended after /run
        # so cwd stays /run; sources must exist (runsc rejects a missing source).
        for dest, source in extra_rw_binds or []:
            os.makedirs(source, exist_ok=True)
            rw_binds.append((dest, source))
        # ``bus_socket`` is the host path for the per-run workflow bus. Its
        # directory is bind-mounted at the fixed in-sandbox mount point
        # (IN_SANDBOX_BUS_DIR, NOT under /run so it can't shadow the file channel),
        # ``--host-uds=open`` is appended to the runsc argv (probe-confirmed — the
        # ONLY flag change), and VC_BUS_SOCK is set so the in-sandbox engine
        # connects to IN_SANDBOX_BUS_SOCK.
        env = dict(env or {})
        if bus_socket is not None:
            bus_host_dir = os.path.dirname(bus_socket)
            os.makedirs(bus_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_BUS_DIR, bus_host_dir))
            env[_BUS_SOCK_ENV] = IN_SANDBOX_BUS_SOCK
        # ``egress_socket`` is the host path for the per-run egress UDS. Its
        # HOST per-run socket path; its DIR is bind-mounted rw at the fixed
        # in-sandbox mount point (IN_SANDBOX_EGRESS_DIR, NOT under /run so it can't
        # shadow the file channel), ``--host-uds=open`` is appended (so the
        # in-sandbox proxy can connect the bound UDS), and VC_EGRESS_SOCK/PORT are
        # set so the in-sandbox proxy listens on 127.0.0.1:PORT + dials the broker.
        # NOTE: the proxy env (HTTP_PROXY/...) + ``--network=none`` are set by the
        # CALLER (run_workflow's egress setup) — run() only wires the UDS + flag.
        if egress_socket is not None:
            egress_host_dir = os.path.dirname(egress_socket)
            os.makedirs(egress_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env[_EGRESS_SOCK_ENV] = IN_SANDBOX_EGRESS_SOCK
            env[_EGRESS_PORT_ENV] = str(_EGRESS_PROXY_PORT)
        # ``lib_overlay`` is the host path for the content-addressed dependency
        # directory ``{lib_overlay_root}/{key}/py``. Bind it read-only at the fixed
        # in-sandbox ``/opt/lib-overlay`` (DISTINCT from the agent's rw
        # ``/opt/agent-overlay``) and set ``VC_LIB_OVERLAY`` so the in-sandbox
        # engine places the Workflow overlay before the explicitly mounted base
        # third-party packages. ``None`` means only the platform base is used.
        ro_dest_binds: "list[tuple[str, str]]" = []
        if lib_overlay is not None:
            ro_dest_binds.append((IN_SANDBOX_LIB_OVERLAY, lib_overlay))
            env[_LIB_OVERLAY_ENV] = IN_SANDBOX_LIB_OVERLAY
        bundle, state_root, run_id = self._build_bundle(
            command=command,
            env=env,
            rw_binds=rw_binds,
            ro_binds=extra_ro_binds,
            ro_dest_binds=ro_dest_binds,
        )

        argv = [
            *self._runtime_flags(
                network,
                host_uds=bus_socket is not None or egress_socket is not None,
            ),
            f"--root={state_root}",
        ]
        argv += [
            "run",
            "-bundle",
            bundle,
            run_id,
        ]

        started = time.monotonic()
        # Popen + start_new_session so the runsc sentry is its own process
        # group; on timeout we kill the GROUP, not just the leader (N6).
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                stdout, stderr = proc.communicate()
                exit_code = -signal.SIGKILL
            duration_s = time.monotonic() - started
            return SandboxResult(
                exit_code=exit_code,
                stdout=stdout or "",
                stderr=stderr or "",
                duration_s=duration_s,
            )
        finally:
            # Best-effort teardown even on timeout/exception (N3/N6).
            try:
                subprocess.run(
                    [self._runsc, f"--root={state_root}", "delete", run_id],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
            except Exception:
                pass
            shutil.rmtree(bundle, ignore_errors=True)

    def run_mcp_probe(
        self,
        *,
        request: dict,
        timeout: float,
        allow_hosts: "set[str]",
    ) -> dict:
        """Probe one MCP server in a fresh, teardown-guaranteed gVisor process.

        The request file lives in a private host temporary directory bound only
        to this one sandbox.  It may contain stdio environment credentials, so
        it is mode 0600 and is removed with the directory in ``finally``.
        """
        with tempfile.TemporaryDirectory(prefix="vc-mcp-probe-") as run_dir:
            request_path = os.path.join(run_dir, "request.json")
            fd = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(request, handle, ensure_ascii=False)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            env = _workflow_python_env()
            env.update({
                "HOME": "/tmp",
                "XDG_CACHE_HOME": "/tmp/.cache",
                "NPM_CONFIG_CACHE": "/tmp/npm-cache",
                "UV_CACHE_DIR": "/tmp/uv-cache",
            })
            egress = self._sandbox_egress_setup(
                f"mcp-probe-{uuid.uuid4().hex}",
                allow_hosts,
            )
            kwargs: dict = {}
            if egress is not None:
                loop_thread, egress_socket, proxy_env = egress
                env.update(proxy_env)
                kwargs["egress_socket"] = egress_socket
                network: str | None = "none"
            else:
                loop_thread = None
                network = None

            try:
                result = self.run(
                    run_dir=run_dir,
                    command=[
                        sys.executable,
                        "-m",
                        "vibecanvas_api.services.sandbox.mcp_probe_entry",
                    ],
                    env=env,
                    network=network,
                    timeout=max(float(timeout) + 5.0, 10.0),
                    extra_ro_binds=_workflow_python_binds(),
                    **kwargs,
                )
            finally:
                if loop_thread is not None:
                    loop_thread.stop()

            result_path = os.path.join(run_dir, "result.json")
            if os.path.isfile(result_path):
                try:
                    with open(result_path, encoding="utf-8") as handle:
                        payload = json.load(handle)
                    if isinstance(payload, dict):
                        return payload
                except (OSError, ValueError):
                    pass
            if result.exit_code == -signal.SIGKILL:
                return {
                    "status": f"error: handshake timed out after {timeout:g}s",
                    "tool_count": None,
                    "tool_names": None,
                }
            detail = (result.stderr or result.stdout or "sandbox probe failed").strip()
            return {
                "status": f"error: sandbox probe failed: {detail[:1000]}",
                "tool_count": None,
                "tool_names": None,
            }

    def run_workflow(
        self,
        *,
        run_dir: str,
        workflow: dict,
        inputs: dict,
        run_id: str,
        tenant: "str | None" = None,
        timeout: float = 120.0,
        bus_socket: str | None = None,
        allow_hosts: "set[str] | None" = None,
        kind: str = "workflow",
        lib_overlay: str | None = None,
        mount_dir: str | None = None,
    ) -> EngineRunResult:
        """Run a sandbox-runnable ``workflow`` INSIDE one gVisor sandbox.

        The bind-mounted run-tier (``run_dir`` ↔ ``/run``) is the result channel
        (no socket / stdout protocol): the host drops ``__exec__/workflow.json`` +
        ``inputs.json`` in, the in-sandbox entrypoint runs the graph and writes
        ``__exec__/{result.json,events.ndjson}`` back, and the host reads them
        after the sandbox exits.

        :func:`classify_workflow` admits only engine-native node types. The run
        always uses ``vibecanvas_engine.sandbox_entry`` and receives no database,
        KMS, Redis, Object Store or provider credential. Platform-data nodes
        must use a host broker/Platform MCP instead of entering this path.

        Plan-B egress (B5): when ``config.sandbox_egress_mode == "proxy"`` AND
        ``allow_hosts`` is given, the run is forced to ``--network=none`` and all
        outbound HTTP(S) is tunneled through a per-run host :class:`EgressBroker`
        (allowlist relay) via the in-sandbox forward proxy. In the DEFAULT
        ``"host-network"`` mode — OR when ``allow_hosts is None`` — NONE of that
        happens and this behaves exactly as before (no network change, no proxy
        env, no broker, no extra bind).

        ``lib_overlay`` is the host path of the content-addressed dependency
        overlay (``{lib_overlay_root}/{key}/py``). When set it is bound READ-ONLY
        at ``/opt/lib-overlay`` and ``VC_LIB_OVERLAY`` is set so the in-sandbox
        CodeNode worker pool places declared Workflow packages before the
        platform base package set. ``None`` means only the base set is used.

        Capability ONLY — NOT wired as the default execution path. Cancellation is
        deferred (N3): a sandboxed run is uncancellable until ``timeout`` kills
        the runsc group.
        """
        command, env, binds = self._build_workflow_invocation(
            run_dir=run_dir, workflow=workflow, inputs=inputs,
            run_id=run_id, tenant=tenant, kind=kind,
        )
        logger.warning(
            "workflow_sandbox_launch_start",
            run_id=run_id,
            kind=kind,
            ro_bind_count=len(binds),
            ro_binds=binds,
            py_path_count=len(_workflow_python_paths()),
        )
        exec_dir = os.path.join(run_dir, "__exec__")
        # Pass ``bus_socket`` only when set so no-bus test doubles retain their
        # original ``run(...)`` signature (back-compat for callers that stub run).
        bus_kwargs = {"bus_socket": bus_socket} if bus_socket is not None else {}
        # Pass ``lib_overlay`` only when set so no-overlay test doubles retain
        # the prior ``run(...)`` signature (back-compat for callers that stub run).
        if lib_overlay is not None:
            bus_kwargs["lib_overlay"] = lib_overlay
        if mount_dir is not None:
            bus_kwargs["extra_rw_binds"] = [("/mount", mount_dir)]

        # Set up the egress proxy only when configured and allowlisted.
        # PromptNode/SubAgentNode do not receive provider credentials directly;
        # they call the internal platform model broker instead.  In Docker that
        # origin resolves to a private service address, so an ordinary hostname
        # allowlist is deliberately insufficient (the broker blocks private DNS
        # results to prevent SSRF).  Grant only the configured platform
        # host:port as an exact private target, matching the Agent Runtime path.
        egress = self._sandbox_egress_setup(run_id, allow_hosts)
        if egress is not None:
            loop_thread, egress_socket, proxy_env = egress
            env = {**env, **proxy_env}
            bus_kwargs["egress_socket"] = egress_socket
            # network=none → the sandbox has NO direct network; egress flows ONLY
            # through the broker (over the bound UDS). FORCE it for this run.
            network = "none"
        else:
            loop_thread = None
            # network=None → config.sandbox_network (default "host"); host gives
            # LLM/HTTP egress (PromptNode/HTTPRequestNode). #482 STEP 1.
            network = None

        try:
            res = self.run(
                run_dir=run_dir,
                command=command,
                env=env,
                network=network,
                timeout=timeout,
                extra_ro_binds=binds,
                **bus_kwargs,
            )
        finally:
            # Tear down the per-run egress broker + its loop thread (cleans up the
            # per-run UDS dir). Best-effort + idempotent.
            if loop_thread is not None:
                loop_thread.stop()

        return self._read_engine_result(exec_dir, res)

    def _egress_setup(
        self,
        run_id: str,
        allow_hosts: "set[str] | None",
        *,
        allow_public: bool = False,
        allow_private_targets: "set[tuple[str, int]] | None" = None,
        trusted_proxy_cidrs: "set[str] | None" = None,
        no_proxy: str = "",
    ) -> "tuple[_BrokerLoopThread, str, dict] | None":
        """Plan-B egress (B5) — start the per-run host :class:`EgressBroker` and
        return ``(loop_thread, egress_socket_path, proxy_env)``, or ``None`` when
        proxy mode is OFF / no allowlist given (the dev path: do NOTHING).

        Gated on ``config.sandbox_egress_mode == "proxy"`` AND ``allow_hosts is
        not None``. The broker SERVES for the duration of the (sync, blocking)
        sandbox run on a dedicated background loop thread (see
        :class:`_BrokerLoopThread` for the async-from-sync bridge); the caller
        ``stop()``s it in a ``finally`` after the run. ``proxy_env`` points the
        sandbox HTTP stack at the in-sandbox proxy (127.0.0.1:_EGRESS_PROXY_PORT)
        and tells the proxy where the bound broker UDS is; ``NO_PROXY=""`` so the
        sandbox does NOT inherit any host no-proxy exclusions."""
        if config.sandbox_egress_mode != "proxy" or allow_hosts is None:
            return None
        egress_socket = _egress_socket_path_for(run_id)
        os.makedirs(os.path.dirname(egress_socket), exist_ok=True)
        broker_policy: dict[str, object] = {}
        if allow_public:
            broker_policy["allow_public"] = True
        if allow_private_targets:
            broker_policy["allow_private_targets"] = allow_private_targets
        if trusted_proxy_cidrs:
            broker_policy["trusted_proxy_cidrs"] = trusted_proxy_cidrs
        broker = EgressBroker(
            egress_socket,
            allow_hosts=allow_hosts,
            run_id=run_id,
            **broker_policy,
        )
        loop_thread = _BrokerLoopThread(broker)
        loop_thread.start()  # bind+listen BEFORE the sandbox launches
        proxy_url = f"http://127.0.0.1:{_EGRESS_PROXY_PORT}"
        proxy_env = {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "NO_PROXY": no_proxy,
            # run() ALSO sets VC_EGRESS_SOCK/PORT from egress_socket; we set them
            # here too so they're present even if a caller stubs run() (the proxy
            # reads these to know where to listen + which UDS to dial).
            _EGRESS_SOCK_ENV: IN_SANDBOX_EGRESS_SOCK,
            _EGRESS_PORT_ENV: str(_EGRESS_PROXY_PORT),
        }
        return loop_thread, egress_socket, proxy_env

    @staticmethod
    def _platform_private_target() -> tuple[str, int]:
        """Return the one internal Platform origin trusted by sandbox brokers."""
        origin = urlsplit(config.mcp.platform_internal_base_url)
        host = origin.hostname
        if not host:
            raise ValueError("Platform MCP internal origin is missing its host")
        return host.lower(), origin.port or (443 if origin.scheme == "https" else 80)

    def _sandbox_egress_setup(
        self,
        run_id: str,
        allow_hosts: "set[str] | None",
    ) -> "tuple[_BrokerLoopThread, str, dict] | None":
        """Build the one network policy used by every sandbox workload.

        Provisioning lifetime is deliberately absent from this interface:
        one-shot, resident and restored sandboxes receive identical public and
        private-destination semantics. ``allow_hosts`` only contributes dynamic
        authority in the global allowlist profile.
        """
        hosts = set(config.sandbox_egress_allow_hosts)
        if config.sandbox_egress_policy == "allowlist":
            hosts.update(allow_hosts or ())
        return self._egress_setup(
            run_id,
            hosts,
            allow_public=config.sandbox_egress_policy == "public",
            allow_private_targets={
                self._platform_private_target(),
                *config.sandbox_egress_private_targets,
            },
            trusted_proxy_cidrs=set(config.sandbox_egress_trusted_proxy_cidrs),
            no_proxy="127.0.0.1,localhost",
        )

    def run_node(
        self, *, run_dir: str, node: dict, inputs: dict, run_id: str,
        tenant: "str | None" = None, timeout: float = 120.0,
        extra: dict | None = None,
        bus_socket: str | None = None,
        allow_hosts: "set[str] | None" = None,
    ) -> EngineRunResult:
        """Run ONE node INSIDE one gVisor sandbox (the ``node`` job kind).

        Writes a self-contained ``__exec__/job.json`` ({kind:"node", node, inputs,
        extra}), selects the entrypoint by classifying the node (wrapped as a
        1-node workflow so the host-only / db-egress guard applies), runs, and
        reads ``result.json`` back. Mirrors ``run_workflow`` with a node payload."""
        pseudo_wf = {node.get("node_id") or "node_x": node}
        classify_workflow(pseudo_wf)

        exec_dir = os.path.join(run_dir, "__exec__")
        os.makedirs(exec_dir, exist_ok=True)
        with open(os.path.join(exec_dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"kind": "node", "node": node, "inputs": inputs,
                       "extra": extra or {}}, f, ensure_ascii=False, default=str)

        binds = _workflow_python_binds()
        env = _workflow_python_env()
        command = [
            sys.executable,
            "-m",
            "vibecanvas_engine.sandbox_entry",
            run_id,
        ]

        run_kwargs = {"bus_socket": bus_socket} if bus_socket is not None else {}
        egress = self._sandbox_egress_setup(run_id, allow_hosts)
        if egress is not None:
            loop_thread, egress_socket, proxy_env = egress
            env.update(proxy_env)
            run_kwargs["egress_socket"] = egress_socket
            network = "none"
        else:
            loop_thread = None
            network = None
        try:
            res = self.run(
                run_dir=run_dir,
                command=command,
                env=env,
                network=network,
                timeout=timeout,
                extra_ro_binds=binds,
                **run_kwargs,
            )
        finally:
            if loop_thread is not None:
                loop_thread.stop()
        return self._read_engine_result(exec_dir, res)

    def run_code(
        self, *, run_dir: str, script: str, inputs: dict, run_id: str,
        timeout: float = 120.0,
        extra_ro_binds: "list[str] | tuple[str, ...]" = (),
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        network: str = "egress",
        expose_run: bool = True,
    ) -> EngineRunResult:
        """Run an arbitrary Python ``script`` INSIDE one gVisor sandbox (the
        ``code`` job kind) — the vehicle for agent Skill scripts.

        Writes a self-contained ``__exec__/job.json`` ({kind:"code", script,
        inputs, timeout_s}), runs the SAME engine entrypoint as ``run_node``
        (the in-sandbox ``run_job`` dispatches by ``job.json`` kind), and reads
        ``result.json`` ({final_outputs:{stdout,stderr,exit_code}, error_dict,
        execution_time}) back. Mirrors ``run_node`` with a code payload.

        ``network="egress"`` uses the same global controller as every other
        sandbox workload. ``none`` remains an explicit capability-probe option;
        legacy ``host`` means connectivity is required and never bypasses proxy
        mode. The engine entry is the PURE engine entrypoint (no tenant / no DSN
        forwarded — code never touches the DB)."""
        # Pure Chat code jobs still need the engine's job/result channel, but the
        # channel must not expose the whole workspace tree. When /run is hidden,
        # bind only this narrow host directory at /tmp; user-visible workspace
        # folders are mounted separately at /data, /memory, and /logs.
        channel_root = run_dir if expose_run else os.path.join(run_dir, "__exec_channel")
        exec_dir = os.path.join(channel_root, "__exec__")
        os.makedirs(exec_dir, exist_ok=True)
        with open(os.path.join(exec_dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"kind": "code", "script": script, "inputs": inputs,
                       "timeout_s": timeout}, f, ensure_ascii=False, default=str)

        # Keep Python wiring explicit: editable source roots, if any, go on
        # PYTHONPATH; configured site-packages are appended by the entrypoint
        # through VC_SANDBOX_PYTHON_PATHS. When the per-wf overlay is rw-bound at
        # ``/opt/agent-overlay`` (the install path), prepend its in-sandbox ``py``
        # dir so packages installed by the agent import ahead of the base env.
        py_paths = _workflow_python_paths()
        overlay_py = [
            os.path.join(dest, "py")
            for dest, _src in (extra_rw_binds or [])
            if dest == "/opt/agent-overlay"
        ]
        run_mount = "/run" if expose_run else "/tmp"
        env = _workflow_python_env()
        env["PYTHONPATH"] = os.pathsep.join(overlay_py + py_paths)
        env["VIBECANVAS_RUN_ROOT"] = run_mount
        if overlay_py:
            # A bare ``pip install`` (e.g. from the bash tool) lands in the
            # persistent per-wf overlay (already on PYTHONPATH above) instead of the
            # one-shot instance rootfs, so installed packages survive the run and
            # later cold-boots — no ``--target`` needed by the agent.
            env["PIP_TARGET"] = overlay_py[0]
        command = [sys.executable, "-m", "vibecanvas_engine.sandbox_entry",
                   run_id]

        # Caller-independent (mirrors run_node): the configured Python runtime
        # binds MUST always reach the sandbox, regardless of what the caller
        # passes. Merge them with the caller's extra_ro_binds.
        binds = list(_workflow_python_binds())
        for b in extra_ro_binds:
            if b not in binds:
                binds.append(b)

        run_kwargs: dict[str, object] = {}
        # Connectivity is a workload request, never a transport bypass. Proxy
        # mode always uses network-none + the controller; trusted development
        # host-network mode uses gVisor hostinet.
        wants_egress = network in {"egress", "host"}
        if wants_egress and config.sandbox_egress_mode == "proxy":
            egress = self._sandbox_egress_setup(run_id, set())
            if egress is None:  # pragma: no cover - guarded by proxy mode
                raise RuntimeError("code sandbox egress broker was not configured")
            loop_thread, egress_socket, proxy_env = egress
            env.update(proxy_env)
            run_kwargs["egress_socket"] = egress_socket
            effective_network = "none"
        else:
            loop_thread = None
            effective_network = "host" if wants_egress else network
        try:
            res = self.run(
                run_dir=channel_root,
                command=command,
                env=env,
                network=effective_network,
                timeout=timeout,
                extra_ro_binds=binds,
                extra_rw_binds=extra_rw_binds,
                run_mount=run_mount,
                **run_kwargs,
            )
        finally:
            if loop_thread is not None:
                loop_thread.stop()
        return self._read_engine_result(exec_dir, res)

    def _build_workflow_invocation(
        self,
        *,
        run_dir: str,
        workflow: dict,
        inputs: dict,
        run_id: str,
        tenant: "str | None",
        kind: str = "workflow",
    ) -> "tuple[list[str], dict, list[str]]":
        """Classify + materialize ``__exec__/{workflow,inputs}.json`` + build the
        ``(command, env, ro_binds)`` for an in-sandbox workflow run.

        Factored out of :meth:`run_workflow` so the blocking one-shot path AND the
        non-blocking bus launcher (:meth:`launch_workflow_bus`) share one
        invocation-build (classify guard and exec-file write). Returns the
        command argv, the env dict, and the read-only
        sys.path binds."""
        # Reject host/API nodes before materializing anything.
        classify_workflow(workflow)

        exec_dir = os.path.join(run_dir, "__exec__")
        os.makedirs(exec_dir, exist_ok=True)
        with open(os.path.join(exec_dir, "workflow.json"), "w", encoding="utf-8") as f:
            json.dump(workflow, f, ensure_ascii=False)
        with open(os.path.join(exec_dir, "inputs.json"), "w", encoding="utf-8") as f:
            json.dump(inputs, f, ensure_ascii=False)
        # P0 — the job descriptor the in-sandbox ``run_job`` dispatches on.
        # Default "workflow" keeps the legacy one-shot bundle's behavior; a
        # future caller passes kind="node"/"tool"/"code".
        with open(os.path.join(exec_dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"kind": kind}, f, ensure_ascii=False)

        # Python import wiring: run the selected host interpreter inside gVisor,
        # bind its prefix + configured dependency paths read-only, and only put
        # editable source roots on PYTHONPATH when api/engine are not installed in
        # that Python environment.
        binds = _workflow_python_binds()
        env = _workflow_python_env()

        command = [
            sys.executable,
            "-m",
            "vibecanvas_engine.sandbox_entry",
            run_id,
        ]
        return command, env, binds

    def launch_workflow_bus(
        self,
        *,
        run_dir: str,
        workflow: dict,
        inputs: dict,
        run_id: str,
        bus_socket: str,
        tenant: "str | None" = None,
        allow_hosts: "set[str] | None" = None,
        kind: str = "workflow",
        lib_overlay: str | None = None,
    ) -> "BusRunHandle":
        """Launch a sandbox workflow run without blocking, streaming
        node events over the host↔sandbox UDS bus at ``bus_socket``.

        Lifecycle INVERTED vs :meth:`run_workflow` (which blocks on
        ``communicate``): the runsc process is ``Popen``-ed and a
        :class:`BusRunHandle` returned WITHOUT waiting, so the route can consume
        the bus broker LIVE while the sandbox runs. The host owns the recv
        deadline + teardown (:meth:`stop_run`). ``--host-uds=open`` + the per-run
        socket bind + ``VC_BUS_SOCK`` are wired by :meth:`run`'s bus path, reused
        here via the same bundle/argv build (inlined non-blocking).
        """
        launch_started = time.perf_counter()
        command, env, binds = self._build_workflow_invocation(
            run_dir=run_dir, workflow=workflow, inputs=inputs,
            run_id=run_id, tenant=tenant, kind=kind,
        )

        # Mirror run()'s rw_binds + env + argv assembly, but Popen non-blocking.
        env = dict(env)
        rw_binds = [("/run", run_dir)]
        bus_host_dir = os.path.dirname(bus_socket)
        os.makedirs(bus_host_dir, exist_ok=True)
        rw_binds.append((IN_SANDBOX_BUS_DIR, bus_host_dir))
        env[_BUS_SOCK_ENV] = IN_SANDBOX_BUS_SOCK

        # Plan-B egress (B6): same proxy path as the blocking ``run_workflow``,
        # but the broker must OUTLIVE this non-blocking launch (the run streams
        # over the bus while it serves). Start it here, bind its in-sandbox UDS +
        # env, FORCE ``--network=none``, and ride the loop thread on the returned
        # handle so ``stop_run`` tears it down. No-op (network stays host) when
        # proxy mode is OFF / no allowlist — the bus path's default, unchanged.
        egress = self._sandbox_egress_setup(run_id, allow_hosts)
        if egress is not None:
            loop_thread, egress_socket, proxy_env = egress
            env = {**env, **proxy_env}
            egress_host_dir = os.path.dirname(egress_socket)
            os.makedirs(egress_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env[_EGRESS_SOCK_ENV] = IN_SANDBOX_EGRESS_SOCK
            env[_EGRESS_PORT_ENV] = str(_EGRESS_PROXY_PORT)
            network = "none"
        else:
            loop_thread = None
            network = None  # → config.sandbox_network (default "host").

        # Bind the content-addressed dependency overlay read-only at the
        # FIXED /opt/lib-overlay and set VC_LIB_OVERLAY so the in-sandbox CodeNode
        # worker pool places declared Workflow packages before the platform base
        # set (mirror of run()'s lib_overlay path). ``None`` → base only.
        ro_dest_binds: "list[tuple[str, str]]" = []
        if lib_overlay is not None:
            ro_dest_binds.append((IN_SANDBOX_LIB_OVERLAY, lib_overlay))
            env[_LIB_OVERLAY_ENV] = IN_SANDBOX_LIB_OVERLAY

        bundle, state_root, container_id = self._build_bundle(
            command=command, env=env, rw_binds=rw_binds, ro_binds=binds,
            ro_dest_binds=ro_dest_binds,
        )
        logger.warning(
            "workflow_sandbox_bundle_built",
            run_id=run_id,
            kind=kind,
            elapsed_ms=int((time.perf_counter() - launch_started) * 1000),
            ro_bind_count=len(binds),
        )
        argv = [
            *self._runtime_flags(network, host_uds=True),
            f"--root={state_root}",
            "run",
            "-bundle",
            bundle,
            container_id,
        ]
        # start_new_session so the sandbox is its own process group — stop_run
        # kills the GROUP (the runsc sentry + gofer), not just the leader (N6).
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            logger.warning(
                "workflow_sandbox_process_started",
                run_id=run_id,
                kind=kind,
                elapsed_ms=int((time.perf_counter() - launch_started) * 1000),
                pid=proc.pid,
            )
        except Exception:
            # Launch failed → the broker would otherwise leak (no handle to carry
            # it). Stop it here, then re-raise.
            if loop_thread is not None:
                loop_thread.stop()
            raise
        exec_dir = os.path.join(run_dir, "__exec__")
        return BusRunHandle(
            proc=proc, bundle_dir=bundle, state_root=state_root,
            container_id=container_id, exec_dir=exec_dir,
            egress_loop_thread=loop_thread,
        )

    def launch_agent_runtime_bus(
        self,
        *,
        run_id: str,
        bus_socket: str,
        tenant: str,
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        extra_ro_binds: "list[str | tuple[str, str]] | tuple[str | tuple[str, str], ...]" = (),
        env_overrides: "dict[str, str] | None" = None,
        snapshot: ServeSnapshot | None = None,
    ) -> "BusRunHandle":
        """Launch one Agent Runtime turn inside gVisor.

        Unlike workflow execution, the request and all streamed output travel
        only through the private UDS bus; no model credential or user message is
        materialized in ``/run``.  Workspace/runtime volumes are explicit
        writable binds supplied by the owning ``SandboxSession``.
        """
        env = _workflow_python_env()
        env.update(env_overrides or {})
        env.update(
            {
                "VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX": "1",
                "VIBECANVAS_RUNTIME_TENANT_ID": tenant,
                _BUS_SOCK_ENV: IN_SANDBOX_BUS_SOCK,
            }
        )
        command = [
            sys.executable,
            "-m",
            "vibecanvas_api.services.agent_runtime.sandbox_entry",
        ]

        rw_binds: list[tuple[str, str]] = []
        seen_destinations: set[str] = set()
        for destination, source in extra_rw_binds or []:
            if destination in seen_destinations:
                continue
            # Most writable mounts are directories and may be created lazily.
            # Existing regular files remain valid for generic callers.
            if not os.path.exists(source):
                os.makedirs(source, exist_ok=True)
            elif not (os.path.isdir(source) or os.path.isfile(source)):
                raise ValueError("writable bind source must be a file or directory")
            rw_binds.append((destination, source))
            seen_destinations.add(destination)
        bus_host_dir = os.path.dirname(bus_socket)
        os.makedirs(bus_host_dir, exist_ok=True)
        rw_binds.append((IN_SANDBOX_BUS_DIR, bus_host_dir))
        if not rw_binds:
            raise RuntimeError("agent runtime requires at least one writable bind")

        ro_binds: list[str] = list(_workflow_python_binds())
        ro_dest_binds: list[tuple[str, str]] = []
        for binding in extra_ro_binds:
            if isinstance(binding, tuple):
                if binding not in ro_dest_binds:
                    ro_dest_binds.append(binding)
            elif binding not in ro_binds:
                ro_binds.append(binding)

        # A live Agent Runtime needs model, MCP, browser and tool egress. The
        # production proxy profile keeps runsc network-none but supplies a
        # per-Runtime HTTP(S)/WebSocket relay: public destinations follow the
        # configured policy, while the private Platform MCP/model origin is an
        # exact host:port grant. No database/control-plane subnet is exposed.
        loop_thread = None
        if config.sandbox_egress_mode == "proxy":
            egress = self._sandbox_egress_setup(run_id, set())
            if egress is None:  # pragma: no cover - guarded by proxy mode above
                raise RuntimeError("Agent Runtime egress broker was not configured")
            loop_thread, egress_socket, proxy_env = egress
            egress_host_dir = os.path.dirname(egress_socket)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env.update(proxy_env)
            env["VC_RUNTIME_EGRESS_PROXY"] = proxy_env["HTTP_PROXY"]

        bundle = ""
        state_root = ""
        try:
            bundle, state_root, container_id = self._build_bundle(
                command=command,
                env=env,
                rw_binds=rw_binds,
                ro_binds=ro_binds,
                ro_dest_binds=ro_dest_binds,
            )
            # The resident Agent Runtime is deliberately stopped before a Chat
            # session is checkpointed; only the credential-free file/workflow
            # worker participates in snapshot/restore.  Its network posture is
            # therefore independent from SANDBOX_NETWORK.  In the local
            # host-network egress profile the active Runtime must share
            # sandboxd's network namespace so Codex can reach the private API
            # service and its model endpoint. The production proxy profile uses
            # the per-Runtime UDS relay prepared above.
            runtime_network = (
                "host"
                if config.sandbox_egress_mode == "host-network"
                else "none"
            )
            argv = [
                *self._runtime_flags(runtime_network, host_uds=True),
                f"--root={state_root}",
                "run",
                "-bundle",
                bundle,
                container_id,
            ]
            if snapshot is None:
                proc = subprocess.Popen(
                    argv,
                    # A resident Runtime can emit more than one pipe buffer of
                    # diagnostics over its lifetime. No caller consumes these
                    # streams, so PIPE would eventually block the Runtime itself.
                    # Inherit the API service log instead: output remains available
                    # for phase diagnostics and can never back-pressure the agent.
                    stdout=None,
                    stderr=None,
                    start_new_session=True,
                )
            else:
                if self._rootless:
                    raise RuntimeError(
                        "Agent Runtime snapshot restore requires rootful runsc"
                    )
                if snapshot.kind != "baseline":
                    raise ValueError(
                        "Agent Runtime may restore only a clean baseline snapshot"
                    )
                image_dir = os.path.abspath(snapshot.image_dir)
                image_stat = os.lstat(image_dir)
                if (
                    stat.S_ISLNK(image_stat.st_mode)
                    or not stat.S_ISDIR(image_stat.st_mode)
                    or image_stat.st_uid != os.geteuid()
                ):
                    raise ValueError("Agent Runtime snapshot image is unsafe")
                base = [
                    *self._runtime_flags(runtime_network, host_uds=True),
                    f"--root={state_root}",
                ]
                with tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8"
                ) as log_file:
                    created = subprocess.run(
                        [*base, "create", "-bundle", bundle, container_id],
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=float(config.sandbox_snapshot_restore_timeout_s),
                    )
                    log_file.seek(0)
                    create_detail = log_file.read()[-2000:]
                if created.returncode != 0:
                    raise RuntimeError(
                        "Agent Runtime restore create failed: "
                        f"{create_detail or 'create failed'}"
                    )
                proc = subprocess.Popen(
                    [
                        *base,
                        "restore",
                        f"--image-path={image_dir}",
                        container_id,
                    ],
                    stdout=None,
                    stderr=None,
                    start_new_session=True,
                )
        except Exception:
            if loop_thread is not None:
                loop_thread.stop()
            if bundle:
                shutil.rmtree(bundle, ignore_errors=True)
            if state_root:
                shutil.rmtree(state_root, ignore_errors=True)
            raise
        return BusRunHandle(
            proc=proc,
            bundle_dir=bundle,
            state_root=state_root,
            container_id=container_id,
            exec_dir=bus_host_dir,
            network=runtime_network,
            egress_loop_thread=loop_thread,
        )

    def stop_run(self, handle: "BusRunHandle", *, kill: bool = False) -> None:
        """Tear down a :meth:`launch_workflow_bus` run. Best-effort + IDEMPOTENT.

        ``kill=True`` (cancel / consumer-abandon) SIGKILLs the runsc process GROUP
        FIRST (reuses the one-shot kill — spec FIX-1; the run_dir is RETAINED by
        the caller for debug, FIX-5d). Then ``runsc delete`` the container and
        ``rmtree`` the bundle (NOT the run_dir — that lives outside the bundle and
        is retained/released by RunWorkspace)."""
        force = bool(kill)
        if not force:
            # The owning broker is closed before this method is called. Give the
            # Runtime a short opportunity to close its app-server and exit so a
            # successful Chat/session teardown does not look like a crash.
            try:
                handle.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                force = True
            except Exception:
                force = True
        if force:
            try:
                subprocess.run(
                    [
                        self._runsc,
                        f"--root={handle.state_root}",
                        "kill",
                        handle.container_id,
                        "KILL",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
            except Exception:
                pass
            try:
                handle.proc.wait(timeout=2.0)
            except Exception:
                try:
                    os.killpg(os.getpgid(handle.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        try:
            subprocess.run(
                [self._runsc, f"--root={handle.state_root}", "delete", "--force",
                 handle.container_id],
                capture_output=True, text=True, timeout=3.0,
            )
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(handle.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        shutil.rmtree(handle.bundle_dir, ignore_errors=True)
        # Plan-B egress (B6): tear down the per-run broker loop thread (cleans up
        # the per-run UDS dir). Best-effort + idempotent — ``None`` in the default
        # host-network mode.
        loop_thread = getattr(handle, "egress_loop_thread", None)
        if loop_thread is not None:
            try:
                loop_thread.stop()
            except Exception:
                pass

    def run_serve(
        self,
        *,
        runs_root: str,
        work_dir: str,
        ro_binds: "list[str] | tuple[str, ...]" = (),
        env: dict | None = None,
        network: "str | None" = None,
        command: "list[str] | None" = None,
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        egress_socket: str | None = None,
    ) -> ServeHandle:
        """Boot a LONG-LIVED warm worker (RE-6 Warm T2) — lifecycle INVERTED vs
        :meth:`run`.

        Binds TWO writable dirs — ``runs_root``→``/runs`` (the ObjectStore run
        root, every tenant/run a subpath) and ``work_dir``→``/work`` (the file
        job channel) — plus the host sys.path ``ro_binds``, and launches the
        engine's ``sandbox_entry serve`` loop. The boot + ``import
        vibecanvas_engine`` cost is paid ONCE here; the worker then serves many
        runs over files (no re-boot per run).

        ``command`` can override the default engine serve command for the
        credential-free API file/MCP job dispatcher. It never grants host
        database or provider credentials.

        ``extra_rw_binds`` (Task 4b-i): additional ``[(destination, source), ...]``
        writable binds appended AFTER the ``/runs`` + ``/work`` binds — so the
        agent's fileop worker mounts its files at clean paths (for example
        ``run_dir/data`` → ``/data``). Default
        ``None`` leaves the bundle binds byte-for-byte as before.

        Returns a :class:`ServeHandle` WITHOUT ``communicate``/teardown — the
        bundle + state_root OUTLIVE this call. ``stop_serve`` (at pool.stop) owns
        the ``runsc delete`` + ``rmtree`` that :meth:`run` does in its ``finally``.
        """
        if command is None:
            command = [
                sys.executable,
                "-m",
                "vibecanvas_engine.sandbox_entry",
                "serve",
                "/work",
                "/runs",
            ]
        rw_binds = [("/runs", runs_root), ("/work", work_dir)]
        rw_binds += list(extra_rw_binds or [])
        env = dict(env or {})
        if egress_socket is not None:
            egress_host_dir = os.path.dirname(egress_socket)
            os.makedirs(egress_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env[_EGRESS_SOCK_ENV] = IN_SANDBOX_EGRESS_SOCK
            env[_EGRESS_PORT_ENV] = str(_EGRESS_PROXY_PORT)
        bundle, state_root, run_id = self._build_bundle(
            command=command,
            env=env,
            rw_binds=rw_binds,
            ro_binds=ro_binds,
        )

        argv = [
            *self._runtime_flags(network, host_uds=egress_socket is not None),
            f"--root={state_root}",
            "run",
            "-bundle",
            bundle,
            run_id,
        ]
        # start_new_session so the worker is its own process group — stop_serve
        # kills the GROUP (the runsc sentry + gofer), not just the leader.
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        # NO communicate / NO teardown — the worker is long-lived (lifecycle
        # inverts vs run); stop_serve owns the teardown.
        return ServeHandle(
            proc=proc,
            bundle_dir=bundle,
            state_root=state_root,
            run_id=run_id,
            network=network,
        )

    def checkpoint_serve(
        self,
        handle: ServeHandle,
        *,
        image_dir: str,
        timeout: float | None = None,
    ) -> None:
        """Checkpoint a ready rootful serve worker into an empty private path."""
        if self._rootless:
            raise RuntimeError("checkpoint/restore is unavailable in rootless mode")
        image_path = os.path.abspath(image_dir)
        os.makedirs(image_path, mode=0o700, exist_ok=True)
        image_stat = os.lstat(image_path)
        if stat.S_ISLNK(image_stat.st_mode) or not stat.S_ISDIR(image_stat.st_mode):
            raise ValueError("snapshot image path must be a real directory")
        if image_stat.st_uid != os.geteuid():
            raise PermissionError("snapshot image directory must be owned by sandboxd")
        os.chmod(image_path, 0o700)
        if any(os.scandir(image_path)):
            raise ValueError("snapshot image directory must be empty")
        checkpoint_timeout = (
            float(timeout)
            if timeout is not None
            else float(config.sandbox_snapshot_checkpoint_timeout_s)
        )
        proc = subprocess.run(
            [
                *self._runtime_flags(handle.network),
                f"--root={handle.state_root}",
                "checkpoint",
                f"--image-path={image_path}",
                f"--compression={config.sandbox_snapshot_compression}",
                handle.run_id,
            ],
            capture_output=True,
            text=True,
            timeout=checkpoint_timeout,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "checkpoint failed")[-2000:]
            raise RuntimeError(f"gVisor checkpoint failed: {detail}")
        # `checkpoint` stops the guest workload, but the original `runsc run`
        # command and OCI state may remain until `runsc delete --force`. Do not
        # kill that process group here: doing so can orphan its sandbox/gofer
        # children and make the subsequent restore `create` hang. The pool
        # immediately calls stop_serve(), which owns ordered OCI teardown.

    def restore_serve(
        self,
        *,
        snapshot: ServeSnapshot,
        runs_root: str,
        work_dir: str,
        ro_binds: "list[str] | tuple[str, ...]" = (),
        env: dict | None = None,
        network: "str | None" = None,
        command: "list[str] | None" = None,
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        egress_socket: str | None = None,
    ) -> ServeHandle:
        """Create a fresh OCI container and restore a rootful serve snapshot."""
        if self._rootless:
            raise RuntimeError("checkpoint/restore is unavailable in rootless mode")
        image_dir = os.path.abspath(snapshot.image_dir)
        try:
            image_stat = os.lstat(image_dir)
        except FileNotFoundError:
            raise FileNotFoundError("snapshot image directory is missing")
        if stat.S_ISLNK(image_stat.st_mode) or not stat.S_ISDIR(image_stat.st_mode):
            raise ValueError("snapshot image path must be a real directory")
        if image_stat.st_uid != os.geteuid():
            raise PermissionError("snapshot image directory must be owned by sandboxd")
        if command is None:
            command = [
                sys.executable,
                "-m",
                "vibecanvas_engine.sandbox_entry",
                "serve",
                "/work",
                "/runs",
            ]
        rw_binds = [("/runs", runs_root), ("/work", work_dir)]
        rw_binds += list(extra_rw_binds or [])
        env = dict(env or {})
        if egress_socket is not None:
            egress_host_dir = os.path.dirname(egress_socket)
            os.makedirs(egress_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env[_EGRESS_SOCK_ENV] = IN_SANDBOX_EGRESS_SOCK
            env[_EGRESS_PORT_ENV] = str(_EGRESS_PROXY_PORT)
        bundle, state_root, run_id = self._build_bundle(
            command=command,
            env=env,
            rw_binds=rw_binds,
            ro_binds=ro_binds,
        )
        base = [
            *self._runtime_flags(network, host_uds=egress_socket is not None),
            f"--root={state_root}",
        ]
        try:
            # `runsc create` launches long-lived sandbox/gofer children. They
            # may inherit its stdout/stderr descriptors, so capture_output=True
            # can wait forever for pipe EOF after the foreground CLI has already
            # exited. A regular temporary file retains diagnostics without
            # coupling completion to descendant descriptor lifetime.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log_file:
                created = subprocess.run(
                    [*base, "create", "-bundle", bundle, run_id],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=float(config.sandbox_snapshot_restore_timeout_s),
                )
                log_file.seek(0)
                create_detail = log_file.read()[-2000:]
            if created.returncode != 0:
                raise RuntimeError(
                    f"gVisor restore create failed: {create_detail or 'create failed'}"
                )
            proc = subprocess.Popen(
                [*base, "restore", f"--image-path={image_dir}", run_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except Exception:
            # Cleanup must never replace the authoritative create/restore
            # exception (for example with a second TimeoutExpired).
            try:
                with tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8"
                ) as cleanup_log:
                    subprocess.run(
                        [*base, "delete", "--force", run_id],
                        stdout=cleanup_log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=10.0,
                        check=False,
                    )
            except Exception:
                pass
            shutil.rmtree(bundle, ignore_errors=True)
            raise
        return ServeHandle(
            proc=proc,
            bundle_dir=bundle,
            state_root=state_root,
            run_id=run_id,
            network=network,
        )

    def stop_serve(self, handle: ServeHandle) -> None:
        """Tear down a warm worker (RE-6 Warm T2). Best-effort + IDEMPOTENT — safe
        to call on an already-dead/already-stopped handle.

        1. ``SIGKILL`` the dedicated worker process group.
        2. ``runsc delete --force`` the stopped OCI state (swallow errors).
        3. ``rmtree`` the bundle dir (``ignore_errors`` — the state_root lives
           inside it, so this removes both).

        The process was launched with ``start_new_session=True`` specifically
        to make this ordering bounded. Docker Desktop can block for the full
        timeout when asked to delete a still-running nested ptrace sandbox.
        """
        try:
            process_group = os.getpgid(handle.proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            process_group = None
        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            handle.proc.wait(timeout=2.0)
        except (subprocess.TimeoutExpired, ChildProcessError, OSError):
            pass
        group_alive = process_group is None
        if process_group is not None:
            try:
                os.killpg(process_group, 0)
                group_alive = True
            except ProcessLookupError:
                group_alive = False
            except (PermissionError, OSError):
                group_alive = True
        delete_argv = [
            self._runsc,
            "--root",
            handle.state_root,
            "delete",
            "--force",
            handle.run_id,
        ]
        # With --ignore-cgroups and a private --root inside bundle_dir, once the
        # dedicated process group is gone there is no external OCI resource to
        # clean: removing the private bundle/state tree is sufficient. Calling
        # `runsc delete` in that state costs ~20s per worker on Docker Desktop.
        # Retain it as a fallback whenever the group could not be proven gone.
        deleted = not group_alive
        if group_alive:
            try:
                # As with `create`, avoid PIPE EOF coupling to any helper process
                # runsc keeps alive while deleting OCI state.
                with tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8"
                ) as delete_log:
                    deleted = subprocess.run(
                        delete_argv,
                        stdout=delete_log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=10.0,
                    ).returncode == 0
            except Exception:
                pass
        if not deleted:
            try:
                with tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8"
                ) as retry_log:
                    subprocess.run(
                        delete_argv,
                        stdout=retry_log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=10.0,
                        check=False,
                    )
            except Exception:
                pass
        shutil.rmtree(handle.bundle_dir, ignore_errors=True)

    @staticmethod
    def _read_engine_result(
        exec_dir: str, res: SandboxResult | None,
    ) -> EngineRunResult:
        """Read ``__exec__/{result.json,events.ndjson}`` back from the run-tier.

        If ``result.json`` is missing (the sandbox crashed pre-write) → an
        engine-error result carrying the tail of the sandbox stderr (so "ran with
        node errors" stays distinct from "engine crashed / no result")."""
        result_path = os.path.join(exec_dir, "result.json")
        if not os.path.exists(result_path):
            detail = (
                (res.stderr or "")[-2000:]
                if res is not None
                else "sandbox job completed without result.json"
            )
            return EngineRunResult(
                final_outputs={},
                error_dict={"__engine__": detail},
                execution_time=0.0,
                events=[],
                sandbox=res,
            )
        with open(result_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)

        events: list = []
        events_path = os.path.join(exec_dir, "events.ndjson")
        if os.path.exists(events_path):
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))

        return EngineRunResult(
            final_outputs=parsed.get("final_outputs", {}) or {},
            error_dict=parsed.get("error_dict", {}) or {},
            execution_time=parsed.get("execution_time", 0.0) or 0.0,
            events=events,
            sandbox=res,
        )


# P1 — the spec names the default provider ``ColdBootProvider`` (it spawns a
# fresh instance per run by COLD-BOOTING a bundle; the future SnapshotProvider
# spawns by restore). Today they are the same implementation, so this is a
# semantic alias — callers depend on the name, not the class identity.
ColdBootProvider = RootlessGvisorProvider


class RootfulGvisorProvider(RootlessGvisorProvider):
    """Root-authorized runsc provider with checkpoint/restore support."""

    def __init__(self, runsc_path: str):
        super().__init__(runsc_path)
        self._rootless = False
