"""Filesystem adapter used by an Agent Runtime already inside the sandbox.

Host-side tools historically acquired a ``SandboxSession`` and submitted JSON
file operations to a warm worker.  Once the runtime itself is inside gVisor,
starting another sandbox would be both wasteful and incorrect.  This adapter
keeps the existing tool-facing method contract while executing against the
already-mounted filesystem.  ``/runtime`` is deliberately absent from the
allowed roots: runtime credentials and SDK state are private to adapters, not
agent-readable workspace files.
"""

from __future__ import annotations

import base64
import difflib
import os

from vibecanvas_api.services.sandbox.fileops import run_fileop


def _available_roots() -> list[str]:
    return [
        path
        for path in ("/data", "/memory", "/logs", "/chats", "/mount", "/run")
        if os.path.isdir(path)
    ]


class LocalAgentRuntimeSession:
    """Subset of ``SandboxSession`` consumed by base Agent tools."""

    def __init__(self, roots: list[str] | None = None) -> None:
        self.roots = list(roots or _available_roots())
        if not self.roots:
            raise RuntimeError("agent runtime has no mounted workspace roots")

    async def read_file(self, path: str) -> dict:
        return run_fileop({"op": "read", "path": path}, self.roots)

    async def write_file(self, path: str, content: str) -> dict:
        return run_fileop(
            {"op": "write", "path": path, "content": content}, self.roots
        )

    async def read_bytes(self, path: str) -> dict:
        result = run_fileop({"op": "read_bytes", "path": path}, self.roots)
        if result.get("ok") and "data_b64" in result:
            return {
                "ok": True,
                "data": base64.b64decode(result["data_b64"]),
            }
        return result

    async def write_bytes(self, path: str, data: bytes) -> dict:
        return run_fileop(
            {
                "op": "write_bytes",
                "path": path,
                "data_b64": base64.b64encode(data).decode("ascii"),
            },
            self.roots,
        )

    async def list_dir(self, path: str) -> dict:
        return run_fileop({"op": "list", "path": path}, self.roots)

    async def grep(
        self, pattern: str, path: str, glob: str = "", context: int = 0
    ) -> dict:
        return run_fileop(
            {
                "op": "grep",
                "pattern": pattern,
                "path": path,
                "glob": glob,
                "context": context,
            },
            self.roots,
        )

    async def edit_file(
        self, path: str, old: str, new: str, replace_all: bool = False
    ) -> dict:
        read_result = await self.read_file(path)
        if not read_result.get("ok"):
            return read_result
        if read_result.get("kind") != "text":
            return {"ok": False, "error": "not_text"}
        before = str(read_result.get("content") or "")
        count = before.count(old)
        if count == 0:
            return {"ok": False, "error": "not_found"}
        if count != 1 and not replace_all:
            return {"ok": False, "error": "not_unique"}
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
        write_result = await self.write_file(path, after)
        if not write_result.get("ok"):
            return write_result
        diff = "\n".join(
            difflib.unified_diff(
                before.splitlines(), after.splitlines(), fromfile=path, tofile=path,
                lineterm="",
            )
        )
        return {
            "ok": True,
            "replacements": count if replace_all else 1,
            "diff": diff,
            "content": after,
        }

    async def run_command(self, command: str, *, timeout_s: float = 60) -> dict:
        result = run_fileop(
            {
                "op": "exec",
                "command": command,
                "cwd": self.roots[0],
                "timeout": max(float(timeout_s), 1.0),
            },
            self.roots,
        )
        if not result.get("ok"):
            return {
                "status": "error",
                "stdout": "",
                "stderr": str(result.get("error") or "command failed"),
                "exit_code": 1,
            }
        return {
            "status": "ok" if result.get("exit_code") == 0 else "error",
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "exit_code": result.get("exit_code"),
        }

    async def writeback_vfs(self) -> None:
        # Host SandboxSession performs the durable VFS diff after the runtime
        # process exits; doing it here would duplicate ownership.
        return None

    def schedule_writeback(self) -> None:
        return None
