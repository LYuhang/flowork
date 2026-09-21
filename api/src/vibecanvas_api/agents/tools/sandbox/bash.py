"""bash tool — run a shell command in the current Agent Runtime sandbox.

The Agent Runtime process already runs inside a sandbox, so this tool invokes bash
directly against the mounted workspace. It does not resolve or inject a nested
``SandboxSession``. Network and persistent overlay behavior are properties of
the owning Runtime sandbox.

OUTPUT: same two-channel design as the fs tools (``@tool_output`` + render) —
``content`` is the command's terminal output (stdout, plus a ``[stderr]`` section
when present), the exit_code is the chaining handle on the artifact, and a large
stdout is offloaded to a re-readable ref by the decorator. NEVER raises a Python
exception to the agent loop — a workspace/run failure becomes a clean error
envelope; a non-zero exit is a SUCCESSFUL tool call whose output the agent reads.
"""
from __future__ import annotations

import time

from vibecanvas_api.services.platform_mcp.tool_runtime import tool
import structlog

from vibecanvas_api.agents.tools.decorator import tool_output, ToolError
from vibecanvas_api.agents.tools.render import register_render, Rendered
from vibecanvas_api.agents.tools import workspace_fs

_STDERR_CAP = 2000
logger = structlog.get_logger(__name__)


def _abstract(command: str, exit_code, stdout: str) -> str:
    lines = stdout.count("\n") + (1 if stdout and not stdout.endswith("\n") else 0)
    cmd = command if len(command) <= 120 else command[:117] + "…"
    return f"ran `{cmd}`, exit {exit_code}, {lines} lines"


@register_render("bash")
def _render(raw: dict, ctx) -> Rendered:
    """bash presentation: content = the command's terminal output (stdout, plus a
    trailing ``[stderr]`` section when present) — the agent reads it like a terminal.
    exit_code is the chaining handle on the artifact; a non-zero exit is still a
    successful tool call whose output the agent inspects."""
    stdout = raw.get("stdout", "")
    stderr = (raw.get("stderr") or "")[:_STDERR_CAP]
    exit_code = raw.get("exit_code")
    body = stdout
    if stderr:
        body = (body.rstrip("\n") + "\n" if body else "") + f"[stderr]\n{stderr}"
    return Rendered(content=body, content_type="text/shell",
                    abstract=_abstract(raw.get("command", ""), exit_code, stdout),
                    extras={
                        "command": raw.get("command", ""),
                        "exit_code": exit_code,
                        "stderr": stderr,
                        "duration_ms": raw.get("duration_ms"),
                    })


@tool_output(content_type="text/shell", tool="bash")
async def _bash(command: str, timeout_s: int) -> dict:
    """Run the command directly in the current sandbox Runtime. Returns
    the raw ``{command, stdout, stderr, exit_code}`` payload (the render lays it out
    as terminal output; a large stdout is offloaded by the decorator). A non-zero
    exit is NOT a tool error — it rides the output. Boot/run failures raise a
    ToolError with a CLEAN message (caught by the decorator → error envelope)."""
    started = time.perf_counter()
    logger.warning(
        "agent_runtime_bash_start",
        timeout_s=timeout_s,
        command_len=len(command or ""),
    )
    try:
        res = await workspace_fs.run_command(command, timeout_s=timeout_s)
    except Exception:  # boot/run failure — clean message, never leak the raw cause
        logger.warning(
            "bash_tool_run_failed",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            exc_info=True,
        )
        raise ToolError("run_failed", "the command could not be run in the workspace sandbox")
    logger.warning(
        "agent_runtime_bash_done",
        exit_code=res.get("exit_code"),
        stdout_chars=len(res.get("stdout") or ""),
        stderr_chars=len(res.get("stderr") or ""),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
    return {"command": command, "stdout": res.get("stdout") or "",
            "stderr": res.get("stderr") or "", "exit_code": res.get("exit_code"),
            "duration_ms": int((time.perf_counter() - started) * 1000)}


@tool(response_format="content_and_artifact")
async def bash(command: str, timeout_s: int = 60) -> str:
    """Run a shell command and return its output.

    The working environment persists across commands — anything you install or
    create stays available to later commands.

    This tool returns stdout/stderr exactly as produced by bash. Many useful
    commands are naturally quiet: downloads, redirects, writes, moves, and some
    validation commands may print nothing on success. When you need to know what
    happened, add explicit status/verification commands to your shell command.

    Good patterns:
    - `python script.py && echo "OK: script completed"`
    - `jq empty /data/workflow.json && echo "OK: valid json"`
    - `curl -fL -S -s -o /data/frame.jpg "$URL" && ls -lh /data/frame.jpg`
    - `curl -fL -S -s -o /data/frame.jpg "$URL" && python - <<'PY'\nfrom pathlib import Path\np=Path('/data/frame.jpg')\nprint(f'OK: {p} {p.stat().st_size} bytes')\nPY`
    - `cat > /data/out.txt <<'EOF'\n...\nEOF\nwc -c /data/out.txt && echo "OK: wrote file"`

    Args:
        command: the shell command to run.
        timeout_s: max seconds to allow (default 60).

    Returns:
        content = the command's output (like a terminal). A non-zero exit is still a
        successful tool call — inspect the output.
    """
    return await _bash(command, timeout_s)
