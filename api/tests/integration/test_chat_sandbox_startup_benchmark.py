"""Opt-in rootful benchmark for a new Chat sandbox with real VFS binds.

Run on a Linux host prepared for rootful gVisor:

    sudo -E FLOWORK_RUN_ROOTFUL_GVISOR_BENCHMARK=1 \
      pytest -q -s api/tests/integration/test_chat_sandbox_startup_benchmark.py

The default run records measurements without enforcing hardware-specific SLOs.
Set FLOWORK_CHAT_SANDBOX_COLD_SLO_S and/or
FLOWORK_CHAT_SANDBOX_RESTORE_SLO_S to turn them into deployment gates.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox.gvisor import (
    RootfulGvisorProvider,
    _workflow_python_binds,
)
from vibecanvas_api.services.sandbox.manager import SandboxSession
from vibecanvas_api.services.sandbox.warm import WarmGvisorPool


_RUNSC = config.runsc_path or shutil.which("runsc")
_ENABLED = os.environ.get("FLOWORK_RUN_ROOTFUL_GVISOR_BENCHMARK") == "1"

pytestmark = [
    pytest.mark.gvisor,
    pytest.mark.skipif(not _ENABLED, reason="rootful benchmark is opt-in"),
    pytest.mark.skipif(os.geteuid() != 0, reason="rootful gVisor requires uid 0"),
    pytest.mark.skipif(not _RUNSC, reason="runsc is not installed"),
]


def _seconds(started: float) -> float:
    return round(time.perf_counter() - started, 4)


@pytest.mark.asyncio
async def test_new_chat_mount_ready_first_tool_and_snapshot_restore(
    tmp_path, monkeypatch,
):
    """Measure the user-visible new-Chat path and validate mounted VFS state."""
    snapshot_root = tmp_path / "snapshots"
    monkeypatch.setattr(config, "sandbox_resident_mode", "snapshot")
    monkeypatch.setattr(config, "sandbox_snapshot_root", str(snapshot_root))
    monkeypatch.setattr(config, "sandbox_snapshot_max_count", 8)
    monkeypatch.setattr(config, "sandbox_snapshot_max_bytes", 1024 * 1024 * 1024)

    prepare_started = time.perf_counter()
    workspace = tmp_path / "workspace"
    mount = tmp_path / "mount"
    runtime = tmp_path / "runtime"
    runs = tmp_path / "runs"
    work = tmp_path / "channel"
    overlay = tmp_path / "overlay"
    for path in (
        workspace / "data",
        workspace / "memory",
        workspace / "logs",
        mount,
        runtime,
        runs,
        work,
        overlay,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (workspace / "data" / "seed.txt").write_text("seed", encoding="utf-8")
    vfs_prepare_s = _seconds(prepare_started)

    provider = RootfulGvisorProvider(str(_RUNSC))
    session = SandboxSession(
        tenant_id="benchmark-tenant",
        wf_id="benchmark-chat",
        run_dir=str(workspace),
        overlay_dir=str(overlay),
        provider=provider,
        base_binds=_workflow_python_binds(),
        mount_dir=str(mount),
        runtime_dir=str(runtime),
        expose_run=False,
        pool_runs_root=str(runs),
        # The benchmark owns this temporary projection directly. Marking that
        # ownership explicitly lets SandboxSession.close() remove it without
        # asking the configured Object Store to release an unrelated prefix.
        materialized_projection_root=str(workspace),
    )
    # The benchmark exercises real shared bind semantics. Durable Object Store
    # writeback has its own integration suite and would obscure startup timing.
    session.writeback_vfs = AsyncMock()
    pool = WarmGvisorPool(
        provider=provider,
        store_root=str(tmp_path / "object-store"),
        work_root=str(work),
        size=2,
        fileops=True,
        fileop_binds=session._rw_binds,
        fileop_roots=["/data", "/memory", "/logs", "/mount"],
        tenant="benchmark-tenant",
        materialized_runs_root=str(runs),
    )
    session._fileop_pool = pool

    try:
        cold_started = time.perf_counter()
        await asyncio.to_thread(pool.start)
        cold_channel_ready_s = _seconds(cold_started)

        first_tool_started = time.perf_counter()
        first = await session.run_command(
            "test \"$(cat /data/seed.txt)\" = seed && "
            "printf memory > /memory/state.txt && "
            "printf log > /logs/startup.log && "
            "printf mount > /mount/shared.txt",
            timeout_s=30,
        )
        first_tool_s = _seconds(first_tool_started)
        assert first["exit_code"] == 0, first

        checkpoint_started = time.perf_counter()
        assert await session.hibernate() is True
        checkpoint_s = _seconds(checkpoint_started)

        restore_started = time.perf_counter()
        assert await session.resume() is True
        restore_channel_ready_s = _seconds(restore_started)

        restored_tool_started = time.perf_counter()
        restored = await session.run_command(
            "test \"$(cat /memory/state.txt)\" = memory && "
            "test \"$(cat /mount/shared.txt)\" = mount && "
            "printf restored >> /logs/startup.log",
            timeout_s=30,
        )
        restored_first_tool_s = _seconds(restored_tool_started)
        assert restored["exit_code"] == 0, restored
        assert (mount / "shared.txt").read_text(encoding="utf-8") == "mount"
        assert (workspace / "logs" / "startup.log").read_text(
            encoding="utf-8"
        ) == "logrestored"

        metrics = {
            "vfs_prepare_s": vfs_prepare_s,
            "cold_channel_ready_s": cold_channel_ready_s,
            "first_tool_s": first_tool_s,
            "new_chat_to_first_tool_s": round(
                vfs_prepare_s + cold_channel_ready_s + first_tool_s, 4
            ),
            "checkpoint_s": checkpoint_s,
            "restore_channel_ready_s": restore_channel_ready_s,
            "restored_first_tool_s": restored_first_tool_s,
            "restore_to_first_tool_s": round(
                restore_channel_ready_s + restored_first_tool_s, 4
            ),
            "configured_mount_limit": config.sandbox_max_mounts,
        }
        artifact = tmp_path / "chat-sandbox-startup.json"
        artifact.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
        print("FLOWORK_CHAT_SANDBOX_BENCHMARK=" + json.dumps(metrics, sort_keys=True))

        cold_slo = os.environ.get("FLOWORK_CHAT_SANDBOX_COLD_SLO_S")
        restore_slo = os.environ.get("FLOWORK_CHAT_SANDBOX_RESTORE_SLO_S")
        if cold_slo:
            assert metrics["new_chat_to_first_tool_s"] <= float(cold_slo)
        if restore_slo:
            assert metrics["restore_to_first_tool_s"] <= float(restore_slo)
    finally:
        await session.close()
