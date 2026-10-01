"""Host client for the in-sandbox workflow control socket.

Transport failure is deliberately distinct from an execution failure. Callers
must reconcile the same invocation before considering any resubmission.
"""

from __future__ import annotations

import asyncio
import contextlib

from vibecanvas_engine.sandbox_bus import encode_frame, read_frame


class WorkflowRpcError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WorkflowRpcClient:
    def __init__(self, socket_path: str, token: str, *, generation: str | None = None):
        self.socket_path = socket_path
        self._token = token
        self.generation = generation

    async def call(self, method: str, *, timeout: float = 30, **args) -> dict:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
            try:
                writer.write(
                    encode_frame({"token": self._token, "generation": self.generation, "method": method, "args": args})
                )
                await writer.drain()
                response = await read_frame(reader, max_len=64 * 1024 * 1024)
                if response is None:
                    raise ConnectionError("workflow RPC disconnected")
                if not response.get("ok"):
                    raise WorkflowRpcError(response.get("error", "runtime_error"))
                result = response["result"]
                if method == "hello":
                    self.generation = result["generation"]
                return result
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
