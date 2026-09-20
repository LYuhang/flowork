"""Private JSONL client for the Codex app-server protocol.

The protocol is deliberately terminated inside the Runtime/auth adapters. Raw
Codex request, response, and notification objects never enter the product
event API or frontend contracts.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

_CODEX_FILE_CREDENTIAL_CONFIG = 'cli_auth_credentials_store="file"'
_CODEX_OUTER_SANDBOX_CONFIG = 'sandbox_mode="danger-full-access"'
_DEFAULT_CODEX_JSONL_READ_LIMIT_BYTES = 16 * 1024 * 1024


def _jsonl_read_limit_bytes() -> int:
    raw = os.environ.get("CODEX_APP_SERVER_JSONL_LIMIT_BYTES", "").strip()
    if not raw:
        return _DEFAULT_CODEX_JSONL_READ_LIMIT_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "CODEX_APP_SERVER_JSONL_LIMIT_BYTES must be an integer"
        ) from exc
    if value < 64 * 1024:
        raise RuntimeError(
            "CODEX_APP_SERVER_JSONL_LIMIT_BYTES must be at least 65536"
        )
    return value


class CodexAppServerError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class CodexAppServer:
    """Own one initialized Codex app-server subprocess."""

    def __init__(
        self,
        *,
        executable: str,
        env: dict[str, str],
        cwd: str | None = None,
        experimental: bool = False,
        outer_sandboxed: bool = False,
        config_overrides: tuple[str, ...] = (),
        read_limit_bytes: int | None = None,
    ) -> None:
        self._executable = executable
        self._env = dict(env)
        self._cwd = cwd
        self._experimental = experimental
        self._outer_sandboxed = outer_sandboxed
        self._config_overrides = tuple(
            value for value in config_overrides if value.strip()
        )
        self._read_limit_bytes = (
            _jsonl_read_limit_bytes()
            if read_limit_bytes is None
            else max(64 * 1024, int(read_limit_bytes))
        )
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._messages: asyncio.Queue[
            dict[str, Any] | CodexAppServerError | None
        ] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._next_id = 1

    async def start(self) -> None:
        if self._process is not None:
            if self._process.returncode is None:
                return
            await self.close()
        arguments = [self._executable, "-c", _CODEX_FILE_CREDENTIAL_CONFIG]
        if self._outer_sandboxed:
            # The Agent Runtime already runs inside a dedicated gVisor
            # container. Asking Codex to create a second Linux sandbox here
            # would require bubblewrap inside gVisor without adding a security
            # boundary, and can make startup fail before the per-thread policy
            # is applied.
            arguments.extend(("-c", _CODEX_OUTER_SANDBOX_CONFIG))
        for override in self._config_overrides:
            arguments.extend(("-c", override))
        arguments.extend(("app-server", "--stdio"))
        self._process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env,
            cwd=self._cwd,
            # thread/resume responses contain the native thread projection in
            # one JSONL record. The asyncio subprocess default is only 64 KiB,
            # which disconnects healthy multi-turn chats before turn/start.
            # Keep a finite, configurable ceiling while allowing realistic
            # Codex histories.
            limit=self._read_limit_bytes,
        )
        # A previously exited process may have left a terminal marker behind.
        # Every new process owns a fresh notification stream.
        self._messages = asyncio.Queue()
        if self._process.stdin is None or self._process.stdout is None:
            await self.close()
            raise CodexAppServerError("codex_app_server_transport_unavailable")
        self._reader_task = asyncio.create_task(self._read_loop())
        capabilities = {"experimentalApi": True} if self._experimental else {}
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "vibecanvas",
                    "title": "Flowork",
                    "version": "1",
                },
                **({"capabilities": capabilities} if capabilities else {}),
            },
            timeout_s=20.0,
        )
        await self.notify("initialized", {})

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise CodexAppServerError("codex_app_server_disconnected")
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(encoded)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise CodexAppServerError("codex_app_server_disconnected") from exc

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float = 60.0,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        request_id = self._next_id
        self._next_id += 1
        future = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send({"method": method, "id": request_id, "params": params or {}})
            message = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise CodexAppServerError("codex_app_server_request_timed_out", method) from exc
        finally:
            self._pending.pop(request_id, None)
        error = message.get("error")
        if error is not None:
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise CodexAppServerError("codex_app_server_request_failed", str(detail or method))
        result = message.get("result")
        if not isinstance(result, dict):
            raise CodexAppServerError("codex_app_server_invalid_response", method)
        return result

    async def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        await self._send({"id": request_id, "result": result})

    async def respond_error(
        self,
        request_id: str | int,
        *,
        code: int,
        message: str,
    ) -> None:
        """Close an unsupported server request without exposing its payload."""
        await self._send({
            "id": request_id,
            "error": {"code": int(code), "message": str(message)[:500]},
        })

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            message = await self._messages.get()
            if message is None:
                return
            if isinstance(message, CodexAppServerError):
                raise message
            yield message

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        failure: Exception | None = None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id in self._pending and "method" not in message:
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                await self._messages.put(message)
        except Exception as exc:  # pragma: no cover - subprocess/pipe failure
            if "chunk is longer than limit" in str(exc):
                failure = CodexAppServerError(
                    "codex_app_server_message_too_large",
                    (
                        "Codex app-server emitted a JSONL record larger than "
                        f"{self._read_limit_bytes} bytes"
                    ),
                )
            else:
                failure = exc
        finally:
            process = self._process
            returncode = getattr(process, "returncode", None)
            if process is not None and returncode is None:
                wait = getattr(process, "wait", None)
                if wait is not None:
                    try:
                        returncode = await asyncio.wait_for(wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        returncode = None
            error = (
                failure
                if isinstance(failure, CodexAppServerError)
                else CodexAppServerError(
                    "codex_app_server_disconnected",
                    str(
                        failure
                        or (
                            "Codex app-server closed its output stream"
                            + (
                                f" (exit status {returncode})"
                                if returncode is not None
                                else ""
                            )
                        )
                    ),
                )
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            # Preserve the transport failure for the active Turn instead of
            # silently presenting an ordinary end-of-iterator. The adapter can
            # now persist a stable Runtime error and logs include the harmless
            # process exit status without exposing stderr or conversation data.
            await self._messages.put(error)

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()


__all__ = [
    "CodexAppServer",
    "CodexAppServerError",
    "_CODEX_FILE_CREDENTIAL_CONFIG",
]
