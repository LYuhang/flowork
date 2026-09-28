# -*- coding: utf-8 -*-
"""Create and restore the credential-free baseline workflow snapshot.

Interactive Chat and Workflow Debug sessions are owned by the unified sandbox
manager. This module contains only the immutable worker snapshot used to reduce
cold-start cost for one-shot background Workflow runs.
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
import uuid
from dataclasses import dataclass
from typing import Any

from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox import get_sandbox_provider
from vibecanvas_api.services.sandbox.session_lifecycle import SnapshotKind
from vibecanvas_api.services.sandbox.snapshot_store import (
    snapshot_category_root,
    snapshot_entries,
    snapshot_tree_bytes,
)


@dataclass
class SandboxHandle:
    """A handle to an acquired serve-parallel sandbox.

    ``work_dir`` is the file job channel root (host writes ``inbox/{id}.json`` +
    ``.ready``, reads ``outbox/{id}.done`` + ``.result.json``). ``runs_root`` is
    the run-tier root the in-sandbox loop resolves ``run_subpath`` against.
    ``_serve`` is the opaque teardown token (a provider ServeHandle) or None.
    """
    work_dir: str
    runs_root: str
    _serve: Any = None


def _serve_parallel_command(concurrency: int) -> list:
    """The in-sandbox entrypoint argv for the parallel serve loop. ``/work`` and
    ``/runs`` are the fixed in-sandbox mount points the provider binds."""
    return [sys.executable, "-m", "vibecanvas_api.sandbox_entry",
            "serve-parallel", "/work", "/runs", str(concurrency)]


def _make_work_dir() -> str:
    work_dir = tempfile.mkdtemp(prefix="bg-work-")
    os.makedirs(os.path.join(work_dir, "inbox"), exist_ok=True)
    os.makedirs(os.path.join(work_dir, "outbox"), exist_ok=True)
    return work_dir


def _probe_serve_handle(handle: SandboxHandle, *, timeout: float) -> None:
    """Prove that a restored worker can see its newly supplied channel mounts."""
    job_id = uuid.uuid4().hex
    inbox = os.path.join(handle.work_dir, "inbox")
    outbox = os.path.join(handle.work_dir, "outbox")
    descriptor = os.path.join(inbox, f"{job_id}.json")
    ready = os.path.join(inbox, f"{job_id}.ready")
    with open(descriptor, "x", encoding="utf-8") as output:
        json.dump(
            {
                "kind": "fileop",
                "op": {
                    "op": "exec",
                    "command": "python -c 'print(\"snapshot-channel-ready\")'",
                    "cwd": "/runs",
                    "timeout": 30,
                },
            },
            output,
        )
    temporary = ready + ".tmp"
    with open(temporary, "x", encoding="ascii"):
        pass
    os.replace(temporary, ready)
    done = os.path.join(outbox, f"{job_id}.done")
    result_path = os.path.join(outbox, f"{job_id}.result.json")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.isfile(done):
            with open(result_path, "r", encoding="utf-8") as result_file:
                result = json.load(result_file)
            for path in (
                done,
                result_path,
                descriptor,
                ready,
                os.path.join(inbox, f"{job_id}.taken"),
            ):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            if (
                int(result.get("exit_code", 1)) != 0
                or "snapshot-channel-ready" not in str(result.get("stdout") or "")
            ):
                raise RuntimeError("restored snapshot channel probe returned an error")
            return
        if handle._serve.proc.poll() is not None:
            raise RuntimeError("restored snapshot worker exited during channel probe")
        time.sleep(0.05)
    raise TimeoutError("restored snapshot channel probe timed out")


def _serve_binds_and_env() -> tuple:
    """Boot wiring for a serve-parallel worker.

    Use the same explicit Python-runtime policy as the warm pool: bind the
    selected Python prefix + configured dependency paths, put only editable source
    roots on PYTHONPATH. Host database, KMS, Redis, and model credentials are
    intentionally absent from the sandbox environment.
    """
    from vibecanvas_api.services.sandbox.gvisor import (
        _workflow_python_binds,
        _workflow_python_env,
    )

    ro_binds = list(_workflow_python_binds())
    # Batch sandboxes are acquired before row jobs are submitted. Mount the
    # public-package cache root once so the selected content-addressed overlay
    # can be injected per job without rebuilding the sandbox.
    overlay_root = os.path.abspath(config.lib_overlay_root)
    os.makedirs(overlay_root, exist_ok=True)
    if overlay_root not in ro_binds:
        ro_binds.append(overlay_root)
    env = _workflow_python_env()
    return ro_binds, env


class SnapshotLifecycle:
    """Restore short-lived workers from an immutable rootful gVisor snapshot."""

    _lock = threading.Lock()

    @staticmethod
    def _tree_bytes(root: str) -> int:
        return sum(
            os.lstat(os.path.join(current, name)).st_size
            for current, directories, files in os.walk(root, followlinks=False)
            for name in files
            if not os.path.islink(os.path.join(current, name))
        )

    @staticmethod
    def _fingerprint(
        provider: Any,
        *,
        command: list[str],
        env: dict[str, str],
        ro_binds: list[str],
        extra_rw_binds: "list[tuple[str, str]] | None",
    ) -> str:
        try:
            runsc_version = subprocess.check_output(
                [provider._runsc, "--version"], text=True, timeout=10.0
            ).strip()
        except Exception as exc:
            raise RuntimeError("unable to identify runsc for snapshot creation") from exc
        source_files = [
            os.path.abspath(__file__),
            os.path.join(os.path.dirname(__file__), "gvisor.py"),
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "sandbox_entry.py"),
        ]
        source_hashes: dict[str, str] = {}
        for path in source_files:
            normalized = os.path.abspath(path)
            if os.path.isfile(normalized):
                with open(normalized, "rb") as source:
                    source_hashes[os.path.basename(normalized)] = hashlib.sha256(
                        source.read()
                    ).hexdigest()
        payload = {
            "format": 1,
            "runsc": runsc_version,
            "python": sys.version,
            "command": command,
            "env": sorted(env.items()),
            "ro_binds": sorted(map(os.path.abspath, ro_binds)),
            "rw_destinations": sorted(dest for dest, _ in extra_rw_binds or []),
            "source": source_hashes,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _wait_ready(handle: SandboxHandle, ready_path: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.isfile(ready_path):
                return
            return_code = handle._serve.proc.poll()
            if return_code is not None:
                stderr = handle._serve.proc.stderr.read() if handle._serve.proc.stderr else ""
                raise RuntimeError(
                    f"snapshot baseline exited before ready (status {return_code}): "
                    f"{stderr[-2000:]}"
                )
            time.sleep(0.05)
        raise TimeoutError("snapshot baseline did not become ready before timeout")

    def _ensure_snapshot(
        self,
        *,
        provider: Any,
        runs_root: str,
        work_dir: str,
        command: list[str],
        concurrency: int,
        ro_binds: list[str],
        env: dict[str, str],
        extra_rw_binds: "list[tuple[str, str]] | None",
    ) -> Any:
        from vibecanvas_api.services.sandbox import ServeSnapshot

        fingerprint = self._fingerprint(
            provider,
            command=command,
            env=env,
            ro_binds=ro_binds,
            extra_rw_binds=extra_rw_binds,
        )
        # Clean, reusable startup snapshots have a different ownership and
        # retention policy from workspace-owned hibernation images. Never place the
        # two kinds in one flat namespace.
        snapshot_root = snapshot_category_root(SnapshotKind.BASELINE)
        final_dir = os.path.join(snapshot_root, fingerprint)
        complete = os.path.join(final_dir, ".complete")

        def _cached_snapshot() -> Any | None:
            try:
                final_stat = os.lstat(final_dir)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(final_stat.st_mode) or not stat.S_ISDIR(final_stat.st_mode):
                raise ValueError("snapshot cache entry must be a real directory")
            image_path = os.path.join(final_dir, "image")
            try:
                image_stat = os.lstat(image_path)
                marker_stat = os.lstat(complete)
            except FileNotFoundError:
                return None
            if (
                stat.S_ISLNK(image_stat.st_mode)
                or not stat.S_ISDIR(image_stat.st_mode)
                or stat.S_ISLNK(marker_stat.st_mode)
                or not stat.S_ISREG(marker_stat.st_mode)
            ):
                raise ValueError("snapshot cache entry has an unsafe shape")
            with open(complete, "r", encoding="ascii") as marker:
                if marker.read().strip() != fingerprint:
                    raise ValueError("snapshot cache fingerprint marker is invalid")
            if not any(os.scandir(image_path)):
                raise ValueError("snapshot cache image is empty")
            age = max(0.0, time.time() - marker_stat.st_mtime)
            if age > config.sandbox_workflow_snapshot_ttl_s:
                return None
            return ServeSnapshot(image_path, fingerprint)

        cached = _cached_snapshot()
        if cached is not None:
            return cached

        with self._lock:
            cached = _cached_snapshot()
            if cached is not None:
                return cached
            if os.path.lexists(final_dir):
                final_stat = os.lstat(final_dir)
                if stat.S_ISLNK(final_stat.st_mode) or not stat.S_ISDIR(final_stat.st_mode):
                    raise ValueError("snapshot cache entry must be a real directory")
                shutil.rmtree(final_dir)
            cache_entries = snapshot_entries()
            if len(cache_entries) >= config.sandbox_snapshot_max_count:
                raise RuntimeError("sandbox snapshot count limit reached")
            staging = tempfile.mkdtemp(prefix="snapshot-", dir=snapshot_root)
            os.chmod(staging, 0o700)
            image_dir = os.path.join(staging, "image")
            baseline = provider.run_serve(
                runs_root=runs_root,
                work_dir=work_dir,
                command=command,
                ro_binds=ro_binds,
                env=env,
                extra_rw_binds=extra_rw_binds,
                network="none",
            )
            baseline_handle = SandboxHandle(
                work_dir=work_dir,
                runs_root=runs_root,
                _serve=baseline,
            )
            try:
                self._wait_ready(
                    baseline_handle,
                    os.path.join(work_dir, "ready"),
                    config.sandbox_snapshot_ready_timeout_s,
                )
                provider.checkpoint_serve(baseline, image_dir=image_dir)
                total_bytes = snapshot_tree_bytes(cache_entries)
                total_bytes += self._tree_bytes(staging)
                if total_bytes > config.sandbox_snapshot_max_bytes:
                    raise RuntimeError("sandbox snapshot byte limit reached")
                with open(os.path.join(staging, ".complete"), "x", encoding="ascii") as marker:
                    marker.write(fingerprint + "\n")
                os.chmod(os.path.join(staging, ".complete"), 0o600)
                os.replace(staging, final_dir)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                provider.stop_serve(baseline)
                try:
                    os.remove(os.path.join(work_dir, "ready"))
                except FileNotFoundError:
                    pass
            return ServeSnapshot(os.path.join(final_dir, "image"), fingerprint)

    def acquire(
        self,
        *,
        runs_root: str,
        concurrency: int,
        tenant: "str | None" = None,
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
    ) -> SandboxHandle:
        provider = get_sandbox_provider()
        if getattr(provider, "_rootless", True):
            raise RuntimeError("snapshot lifecycle requires rootful gVisor")
        work_dir = _make_work_dir()
        ro_binds, env = _serve_binds_and_env()
        command = _serve_parallel_command(concurrency)
        serve = None
        try:
            snapshot = self._ensure_snapshot(
                provider=provider,
                runs_root=runs_root,
                work_dir=work_dir,
                command=command,
                concurrency=concurrency,
                ro_binds=ro_binds,
                env=env,
                extra_rw_binds=extra_rw_binds,
            )
            serve = provider.restore_serve(
                snapshot=snapshot,
                runs_root=runs_root,
                work_dir=work_dir,
                command=command,
                ro_binds=ro_binds,
                env=env,
                extra_rw_binds=extra_rw_binds,
                network="none",
            )
            handle = SandboxHandle(
                work_dir=work_dir, runs_root=runs_root, _serve=serve
            )
            _probe_serve_handle(
                handle,
                timeout=float(config.sandbox_snapshot_restore_timeout_s),
            )
            return handle
        except Exception:
            if serve is not None:
                provider.stop_serve(serve)
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    def release(self, handle: SandboxHandle) -> None:
        if handle._serve is not None:
            get_sandbox_provider().stop_serve(handle._serve)
        shutil.rmtree(handle.work_dir, ignore_errors=True)
