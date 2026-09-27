"""Direct, cancellable shell execution for the Workflow SubAgent."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile

from vibecanvas_api.agents.tools import workspace_fs

_INLINE_CHARS = 24000


async def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


def _result(payload: dict) -> tuple[str, dict]:
    return json.dumps(payload, ensure_ascii=False), payload


def _output(path: Path) -> dict:
    # Output is streamed to disk while running, not accumulated in process RAM.
    with path.open(encoding="utf-8", errors="replace") as stream:
        preview = stream.read(_INLINE_CHARS + 1)
    if len(preview) <= _INLINE_CHARS:
        path.unlink()
        return {"text": preview}
    return {"text": preview[:_INLINE_CHARS], "truncated": True, "file": str(path)}


async def bash(command: str, timeout_s: float | None = None, *, cwd: str | None = None) -> tuple:
    """Execute a shell command in the current Workflow sandbox.

    Use shell commands or Python scripts to read, search, create and edit files.
    Each call starts a new shell in the run directory: file changes persist,
    but cd and shell variables do not carry over to the next call.
    Inspect exit_code, stdout and stderr. Long output is saved to the returned
    file path; read it in portions with a subsequent command.
    timeout_s is optional; no extra command deadline is imposed when omitted.
    Workflow cancellation and sandbox lifetime still apply. Commands are awaited;
    do not leave background processes running after the delegated task finishes.
    """
    if not command.strip() or (timeout_s is not None and timeout_s <= 0):
        return _result({"status": "error", "error": {
            "code": "invalid_arguments", "message": "Provide a command and, if set, a positive timeout_s.",
        }})
    process = None
    paths = []
    try:
        directory = cwd or workspace_fs.roots()[0]
        # Per-invocation files also isolate concurrent nodes. Full output is
        # retained when too long for inline display or when the command fails.
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".subagent-stdout-", suffix=".log", delete=False) as stdout:
            paths.append(Path(stdout.name))
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".subagent-stderr-", suffix=".log", delete=False) as stderr:
                paths.append(Path(stderr.name))
                process = await asyncio.create_subprocess_exec(
                    "/bin/bash", "-c", command, cwd=directory,
                    stdout=stdout, stderr=stderr, start_new_session=True,
                )
                timed_out = False
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    timed_out = True
                    await _terminate(process)
                except asyncio.CancelledError:
                    await _terminate(process)
                    raise
        out, err = (_output(path) for path in paths)
        payload = {
            "exit_code": process.returncode, "stdout": out.pop("text"), "stderr": err.pop("text"),
        }
        if out:
            payload["stdout_file"] = out["file"]
            payload["stdout_truncated"] = True
        if err:
            payload["stderr_file"] = err["file"]
            payload["stderr_truncated"] = True
        if out or err:
            payload["hint"] = "Full output is saved to the returned file paths. Read it in portions."
        if timed_out:
            payload.update(status="error", error={
                "code": "command_timeout",
                "message": f"The command exceeded timeout_s={timeout_s} and was terminated.",
            })
        elif process.returncode != 0:
            payload.update(status="error", error={
                "code": "command_failed", "message": "The command exited unsuccessfully. Inspect stdout and stderr.",
            })
        else:
            payload["status"] = "success"
        return _result(payload)
    except asyncio.CancelledError:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError) as exc:
        for path in paths:
            path.unlink(missing_ok=True)
        return _result({"status": "error", "error": {
            "code": "run_failed", "message": f"Could not execute command: {type(exc).__name__}. Check the run directory and available disk space.",
        }})
