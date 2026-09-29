# -*- coding: utf-8 -*-
"""In-sandbox per-job execution unit for the parallel serve loop (A2).

One job is executed via the credential-free API ``run_one`` delegate. The engine writes
``result.json`` + ``events.ndjson`` under ``{run_root}/__exec__/``, so this unit
only reports execution STATUS — the host reads the actual outputs from those
files.

This is the worker script ``serve_loop_parallel`` spawns through
``BoundedSubprocessPool`` — the whole-workflow analogue of the engine's
``code_worker.py``. It is launched as a standalone script with two inherited
control-pipe fds:

    python <this file> <job_read_fd> <result_write_fd>

Framing matches ``code_worker`` / ``BoundedSubprocessPool``: a 4-byte big-endian
length prefix + a UTF-8 JSON body, over blocking ``os.read`` / ``os.write``.

It lives in the **api** package because the same dispatcher also serves bounded
file/MCP jobs. A workflow tenant is path/audit metadata only; no database
context or credential is created in this process. Worker-import cost is
amortized because the pool reuses workers across jobs.
"""
import json
import os
import struct
import sys

_LEN = struct.Struct(">I")  # 4-byte big-endian unsigned length prefix


def run_job(job: dict) -> dict:
    """Execute ONE job through the unified sandbox entrypoint.

    Returns a result envelope ``{"status": "success"|"error", ...}``. The
    actual outputs live in ``{run_root}/__exec__/result.json``; the host reads
    that file. ``run_one`` dispatches by ``__exec__/job.json`` so workflow, node,
    and code jobs share one protocol.
    """
    from vibecanvas_api.sandbox_entry import run_one

    if job.get("kind") == "prewarm":
        from vibecanvas_engine.workflow import Workflow
        return {"status": "success"}

    try:
        rc = run_one(job["run_root"], job["run_id"], job.get("tenant") or "")
    except Exception as e:
        return {"status": "error", "error_message": str(e)}
    return {"status": "success" if rc == 0 else "error", "exit_code": rc}


def _read_exact(fd: int, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            raise EOFError("job pipe closed")
        buf += chunk
    return buf


def _main(job_read_fd: int, result_write_fd: int) -> None:
    """Serve framed jobs until the parent closes the job pipe (EOF → exit)."""
    while True:
        try:
            (length,) = _LEN.unpack(_read_exact(job_read_fd, _LEN.size))
            job = json.loads(_read_exact(job_read_fd, length))
        except Exception:
            break
        result = run_job(job)
        body = json.dumps(result).encode("utf-8")
        frame = _LEN.pack(len(body)) + body
        view = memoryview(frame)
        total = 0
        while total < len(frame):
            total += os.write(result_write_fd, view[total:])


if __name__ == "__main__":
    _main(int(sys.argv[1]), int(sys.argv[2]))
