"""Clean gVisor bootstrap snapshots for resident Agent Runtimes.

Images stop at a deliberately credential-free boundary: Runtime modules are
imported, but no Chat request, VFS content, auth material, MCP capability, model
capability, bus connection, or network proxy has been opened.  Restore replaces
the fixed mount destinations with one Chat's private host sources.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from vibecanvas_api.config import config

from .bus_broker import IN_SANDBOX_BUS_DIR, socket_path_for
from .gvisor import ServeHandle, ServeSnapshot
from .session_lifecycle import SnapshotKind
from .snapshot_store import (
    snapshot_category_root,
    snapshot_entries,
    snapshot_tree_bytes,
)

_LOCK = threading.Lock()
_REGISTERED: dict[str, ServeSnapshot] = {}


def _tree_bytes(root: str) -> int:
    return sum(
        os.lstat(os.path.join(current, name)).st_size
        for current, directories, files in os.walk(root, followlinks=False)
        for name in files
        if not os.path.islink(os.path.join(current, name))
    )


def _source_hashes() -> dict[str, str]:
    api_root = Path(__file__).resolve().parents[2]
    paths = (
        Path(__file__).resolve(),
        Path(__file__).with_name("gvisor.py").resolve(),
        Path(__file__).with_name("manager.py").resolve(),
        api_root / "services" / "agent_runtime" / "sandbox_entry.py",
        api_root / "services" / "agent_runtime" / "codex.py",
        api_root / "services" / "agent_runtime" / "codex_mcp_hub_gateway.py",
        api_root / "services" / "agent_runtime" / "mcp_hub.py",
        api_root / "services" / "agent_runtime" / "mcp_hub_adapter.py",
        api_root / "services" / "agent_runtime" / "mcp_browser_transport.py",
        api_root / "services" / "agent_runtime" / "mcp_runtime_protocol.py",
        api_root / "services" / "agent_runtime" / "mcp.py",
    )
    return {
        str(path.relative_to(api_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }


def _fingerprint(
    provider: Any,
    *,
    runtime_type: str,
    rw_binds: list[tuple[str, str]],
    ro_binds: list[str | tuple[str, str]],
    env_overrides: dict[str, str],
) -> str:
    try:
        runsc_version = subprocess.check_output(
            [provider._runsc, "--version"], text=True, timeout=10.0
        ).strip()
    except Exception as exc:
        raise RuntimeError("unable to identify runsc for Runtime snapshot") from exc
    payload = {
        "format": 3,
        "kind": "agent_runtime_bootstrap",
        "runtime_type": runtime_type,
        "runsc": runsc_version,
        "python": sys.version,
        "rw_destinations": sorted(destination for destination, _ in rw_binds),
        "ro_bindings": sorted(
            (
                # A destination/source tuple is an intentionally substitutable
                # Chat resource (currently /skills).  Its host source changes
                # for every user and must not turn a reusable baseline into a
                # per-prewarm cache entry. Host-identity code/package mounts,
                # on the other hand, are part of the executable contract.
                [str(value[0]), "<runtime-resource>"]
                if isinstance(value, tuple)
                else [os.path.abspath(str(value)), os.path.abspath(str(value))]
            )
            for value in ro_binds
        ),
        # Values are deployment code/config facts only. Tenant/user values are
        # intentionally absent from the bootstrap environment.
        "environment": sorted(env_overrides.items()),
        "source": _source_hashes(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _cached_snapshot(final_dir: str, fingerprint: str) -> ServeSnapshot | None:
    try:
        directory = os.lstat(final_dir)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode):
        raise ValueError("Runtime snapshot cache entry must be a real directory")
    image_dir = os.path.join(final_dir, "image")
    marker_path = os.path.join(final_dir, ".complete")
    try:
        image = os.lstat(image_dir)
        marker = os.lstat(marker_path)
    except FileNotFoundError:
        return None
    if (
        stat.S_ISLNK(image.st_mode)
        or not stat.S_ISDIR(image.st_mode)
        or stat.S_ISLNK(marker.st_mode)
        or not stat.S_ISREG(marker.st_mode)
    ):
        raise ValueError("Runtime snapshot cache entry has an unsafe shape")
    with open(marker_path, "r", encoding="ascii") as handle:
        if handle.read().strip() != fingerprint:
            raise ValueError("Runtime snapshot fingerprint marker is invalid")
    if not any(os.scandir(image_dir)):
        raise ValueError("Runtime snapshot image is empty")
    age = max(0.0, time.time() - marker.st_mtime)
    if age > config.sandbox_workflow_snapshot_ttl_s:
        return None
    return ServeSnapshot(
        image_dir=image_dir,
        fingerprint=fingerprint,
        kind=SnapshotKind.BASELINE.value,
    )


def get_agent_runtime_baseline(runtime_type: str) -> ServeSnapshot | None:
    """Return the startup-validated image for one Runtime adapter."""

    return _REGISTERED.get(runtime_type)


def ensure_agent_runtime_baseline(
    provider: Any,
    *,
    runtime_type: str,
    rw_binds: list[tuple[str, str]],
    ro_binds: list[str | tuple[str, str]],
    env_overrides: dict[str, str] | None = None,
) -> ServeSnapshot:
    """Build or validate one clean Runtime bootstrap image and register it."""

    if getattr(provider, "_rootless", True):
        raise RuntimeError("Agent Runtime baselines require rootful runsc")
    clean_env = {
        **(env_overrides or {}),
        "VC_AGENT_RUNTIME_TYPE": runtime_type,
        "VC_AGENT_RUNTIME_BOOTSTRAP_READY": (
            f"{IN_SANDBOX_BUS_DIR}/bootstrap.ready"
        ),
    }
    fingerprint = _fingerprint(
        provider,
        runtime_type=runtime_type,
        rw_binds=rw_binds,
        ro_binds=ro_binds,
        env_overrides=clean_env,
    )
    root = snapshot_category_root(SnapshotKind.BASELINE)
    final_dir = os.path.join(root, fingerprint)

    with _LOCK:
        cached = _cached_snapshot(final_dir, fingerprint)
        if cached is not None:
            _REGISTERED[runtime_type] = cached
            return cached
        if os.path.lexists(final_dir):
            info = os.lstat(final_dir)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("Runtime snapshot cache entry is unsafe")
            shutil.rmtree(final_dir)
        entries = snapshot_entries()
        if len(entries) >= config.sandbox_snapshot_max_count:
            raise RuntimeError("sandbox snapshot count limit reached")
        staging = tempfile.mkdtemp(prefix="runtime-bootstrap-", dir=root)
        os.chmod(staging, 0o700)
        image_dir = os.path.join(staging, "image")
        bus_socket = socket_path_for(
            f"runtime-bootstrap:{runtime_type}:{fingerprint}"
        )
        ready_path = os.path.join(os.path.dirname(bus_socket), "bootstrap.ready")
        handle = None
        try:
            handle = provider.launch_agent_runtime_bus(
                run_id=f"runtime-bootstrap-{runtime_type}-{fingerprint[:12]}",
                bus_socket=bus_socket,
                tenant="00000000-0000-0000-0000-000000000000",
                extra_rw_binds=rw_binds,
                extra_ro_binds=ro_binds,
                env_overrides=clean_env,
            )
            deadline = time.monotonic() + config.sandbox_snapshot_ready_timeout_s
            while time.monotonic() < deadline:
                if os.path.isfile(ready_path):
                    break
                if handle.proc.poll() is not None:
                    raise RuntimeError(
                        "Agent Runtime bootstrap exited before ready"
                    )
                time.sleep(0.05)
            else:
                raise TimeoutError("Agent Runtime bootstrap was not ready in time")
            provider.checkpoint_serve(
                ServeHandle(
                    proc=handle.proc,
                    bundle_dir=handle.bundle_dir,
                    state_root=handle.state_root,
                    run_id=handle.container_id,
                    network=handle.network,
                ),
                image_dir=image_dir,
            )
            if snapshot_tree_bytes(entries) + _tree_bytes(staging) > (
                config.sandbox_snapshot_max_bytes
            ):
                raise RuntimeError("sandbox snapshot byte limit reached")
            marker_path = os.path.join(staging, ".complete")
            descriptor = os.open(
                marker_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="ascii") as marker:
                marker.write(fingerprint)
                marker.flush()
                os.fsync(marker.fileno())
            os.replace(staging, final_dir)
            snapshot = ServeSnapshot(
                image_dir=os.path.join(final_dir, "image"),
                fingerprint=fingerprint,
                kind=SnapshotKind.BASELINE.value,
            )
            _REGISTERED[runtime_type] = snapshot
            return snapshot
        finally:
            if handle is not None:
                provider.stop_run(handle, kill=True)
            shutil.rmtree(os.path.dirname(bus_socket), ignore_errors=True)
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
