# -*- coding: utf-8 -*-
"""CodeNode worker shim — runs as ``python -m vibecanvas_engine.code_worker``.

This is the in-subprocess half of the LEAN CodeNode execution model. The selected OS sandbox (the
sandbox the whole workflow runs inside) is the isolation boundary now, so this
shim deliberately has NO in-process jail: user ``process_fn`` code runs with the
NORMAL Python builtins, NO AST check, NO ``open``/``import`` whitelist. The only
job-level safety this layer enforces is being a separate, KILLABLE process so the
parent can impose a hard per-node timeout (SIGKILL a runaway ``while True``).

Control channel (NOT stdin/stdout)
----------------------------------
User code may freely ``print(...)`` or read ``stdin``; that must not corrupt the
job/result protocol. So the parent passes TWO inherited pipe FDs via subprocess
``pass_fds`` and hands their integer numbers in argv:

    python -m vibecanvas_engine.code_worker <job_read_fd> <result_write_fd>

We read framed jobs from ``job_read_fd`` and write framed results to
``result_write_fd``. The process's real ``stdout``/``stderr`` are left alone for
the parent to inherit/log, and AROUND each ``process_fn`` call we temporarily
redirect ``sys.stdout``/``sys.stderr`` to a sink so user prints can never reach
the control FDs (and, defensively, the control FDs are distinct fds anyway).

Framing
-------
One frame = 4-byte big-endian unsigned length prefix + a UTF-8 JSON body. This is
byte-compatible with ``vibecanvas_engine.sandbox_bus`` (same ``>I`` prefix + JSON),
but implemented here with blocking ``os.read``/``os.write`` so the shim stays
STDLIB-ONLY and synchronous (sandbox_bus's helpers are asyncio-based).

STDLIB-BOOTSTRAPPED — this module starts with an empty ``PYTHONPATH`` and must
not import anything outside the standard library during bootstrap. Only after
these imports complete do we append the Workflow overlay and platform base
package paths to ``sys.path``. Appending keeps stdlib authoritative while making
the two controlled third-party tiers available to user code.
"""

import io
import json
import os
import struct
import sys
import traceback


def _install_dependency_overlay():
    """Append controlled dependency directories after the standard library.

    ``PYTHONPATH`` cannot be used for this because CPython places its entries
    before stdlib directories during startup.  An overlay can legitimately
    contain transitive packages with stdlib-colliding names (notably enum34),
    and allowing those to win can crash the worker before it reads a job.
    """
    raw = os.environ.get("VC_CODE_PYTHONPATH", "")
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if entry and entry not in sys.path:
            sys.path.append(entry)


_install_dependency_overlay()

_LEN = struct.Struct(">I")  # 4-byte big-endian unsigned length prefix


def _read_exact(fd, n):
    """Read exactly ``n`` bytes from ``fd``. Returns ``None`` at clean EOF
    (peer closed before any byte of this chunk); raises on a truncated read."""
    buf = bytearray()
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            if not buf:
                return None  # clean EOF at a frame boundary
            raise EOFError("truncated frame on control channel")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(fd):
    """Read one framed message from ``fd``. Returns the decoded object, or
    ``None`` at clean EOF (parent closed the job pipe → worker should exit)."""
    header = _read_exact(fd, _LEN.size)
    if header is None:
        return None
    (length,) = _LEN.unpack(header)
    body = _read_exact(fd, length)
    if body is None:
        raise EOFError("missing frame body on control channel")
    return json.loads(body.decode("utf-8"))


def write_frame(fd, msg):
    """Write one framed message dict to ``fd``."""
    body = json.dumps(msg, ensure_ascii=False, default=str).encode("utf-8")
    frame = _LEN.pack(len(body)) + body
    # os.write may short-write on a pipe; loop until the whole frame is out.
    view = memoryview(frame)
    total = 0
    while total < len(frame):
        total += os.write(fd, view[total:])


def _run_job(code, inputs):
    """Execute one job and return a result envelope dict.

    Runs ``exec(code, ns)`` with NORMAL builtins, pulls ``process_fn`` out of the
    namespace, calls it, and validates the result is a JSON-serializable dict.
    All user-visible stdout/stderr during the call goes to a sink.
    """
    ns = {}
    try:
        exec(code, ns)
    except Exception as e:  # syntax-at-exec / top-level errors in user code
        return {
            "status": "error",
            "error_message": str(e),
            "traceback": traceback.format_exc(),
        }

    process_fn = ns.get("process_fn")
    if process_fn is None:
        return {
            "status": "error",
            "error_message": "The provided code must define a function named 'process_fn'.",
            "traceback": "",
        }
    if not callable(process_fn):
        return {
            "status": "error",
            "error_message": (
                f"'process_fn' must be a callable function, but got "
                f"{type(process_fn).__name__}."
            ),
            "traceback": "",
        }

    # Redirect user code's stdout/stderr to a sink so prints/log noise cannot
    # reach the control channel (and don't pollute the parent's streams).
    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        result = process_fn(inputs)
    except Exception as e:
        return {
            "status": "error",
            "error_message": str(e),
            "traceback": traceback.format_exc(),
        }
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr

    if not isinstance(result, dict):
        return {
            "status": "error",
            "error_message": (
                "process_fn must return a JSON-serializable dict: returned "
                f"{type(result).__name__}"
            ),
            "traceback": "",
        }
    try:
        json.dumps(result)
    except (TypeError, ValueError) as e:
        return {
            "status": "error",
            "error_message": (
                f"process_fn must return a JSON-serializable dict: {e}"
            ),
            "traceback": "",
        }
    return {"status": "success", "output": result}


def main(argv=None):
    argv = sys.argv if argv is None else argv
    job_read_fd = int(argv[1])
    result_write_fd = int(argv[2])

    while True:
        try:
            job = read_frame(job_read_fd)
        except Exception:
            # Unreadable control channel — nothing safe to do but exit.
            break
        if job is None:
            break  # parent closed the job pipe → shut down

        result = _run_job(job.get("code", ""), job.get("inputs", {}))
        try:
            write_frame(result_write_fd, result)
        except Exception:
            # Parent went away mid-write — exit; the parent's read EOF will be
            # interpreted as a crash and the worker respawned.
            break


if __name__ == "__main__":
    main()
