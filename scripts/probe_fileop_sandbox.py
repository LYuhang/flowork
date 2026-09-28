#!/usr/bin/env python
"""Probe the resident fileop sandbox without API/agent/checkpointer.

Usage:
  python scripts/probe_fileop_sandbox.py
  python scripts/probe_fileop_sandbox.py "ls -la /data/"
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import sys

from vibecanvas_api.services.object_store import FilesystemObjectStore
from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox import get_sandbox_provider, SandboxUnavailable
from vibecanvas_api.services.sandbox.warm import WarmGvisorPool
import vibecanvas_api.services.sandbox.warm as warm_mod


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "ls -la /data/"
    print(f"sandbox_runtime={config.sandbox_runtime} sandbox_type={config.sandbox_type}")
    try:
        provider = get_sandbox_provider()
    except SandboxUnavailable as exc:
        print(f"Selected sandbox unavailable: {exc}")
        return 2

    root = tempfile.mkdtemp(prefix="vc-fileop-probe-")
    store_root = os.path.join(root, "store")
    work_root = os.path.join(root, "work")
    data_dir = os.path.join(root, "data")
    memory_dir = os.path.join(root, "memory")
    logs_dir = os.path.join(root, "logs")
    for p in (data_dir, memory_dir, logs_dir):
        os.makedirs(p, exist_ok=True)
    Path(os.path.join(data_dir, "probe.txt")).write_text("hello\n", encoding="utf-8")

    warm_mod.get_object_store = lambda: FilesystemObjectStore(root=store_root)
    pool = WarmGvisorPool(
        provider=provider,
        store_root=store_root,
        work_root=work_root,
        size=config.sandbox_fileop_workers,
        fileops=True,
        db=False,
        fileop_binds=[
            ("/data", data_dir),
            ("/memory", memory_dir),
            ("/logs", logs_dir),
        ],
    )
    try:
        started = time.perf_counter()
        pool.start()
        print(f"pool.start_ms={int((time.perf_counter() - started) * 1000)}")

        for cmd in (command, "cat /data/probe.txt"):
            started = time.perf_counter()
            res = pool.submit_fileop(
                {"op": "exec", "command": cmd, "cwd": "/", "timeout": 60},
                timeout=90,
            )
            print(f"\ncommand={cmd!r}")
            print(f"submit_ms={int((time.perf_counter() - started) * 1000)}")
            print(json.dumps(res, ensure_ascii=False, indent=2)[:4000])
    finally:
        pool.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
