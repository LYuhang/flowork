"""Authenticated, bounded Unix-socket control of a resident workflow runtime.

The bootstrap secret is read once from stdin, never from argv, environment or
files. Children receive neither stdin nor this secret. Each connection carries
one request; a dropped connection never cancels its accepted execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import sys

from ..sandbox_bus import encode_frame, read_frame
from .approvals import ApprovalConflict
from .executions import ExecutionCapacityError, ExecutionConflict, WorkflowRuntime

MAX_FRAME_BYTES = 32 * 1024 * 1024


class RuntimeServer:
    def __init__(self, runtime: WorkflowRuntime, token: str):
        if len(token) < 32:
            raise ValueError("invalid runtime control token")
        self.runtime = runtime
        self._token = token
        self._handlers: set[asyncio.Task] = set()

    async def dispatch(self, method: str, args: dict) -> dict:
        rt = self.runtime
        if method == "hello":
            return {"generation": rt.generation, "capacity": rt.capacity}
        if method == "install":
            rt.install(args["revision"], args["workflow"], node_id=args.get("node_id"))
            return {"generation": rt.generation}
        if method == "invoke":
            return rt.invoke(**args)
        if method == "status":
            return rt.status(**args)
        if method == "events":
            return await rt.events(**args)
        if method == "acknowledge":
            rt.acknowledge(**args)
            return {"ok": True}
        if method == "decide":
            return await rt.decide(**args)
        if method == "resume":
            return rt.resume(**args)
        if method == "cancel":
            return await rt.cancel(**args)
        raise ValueError("unknown runtime operation")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            request = await asyncio.wait_for(read_frame(reader, max_len=MAX_FRAME_BYTES), 10)
            if not isinstance(request, dict):
                request = {}
            supplied = request.get("token", "")
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self._token):
                response = {"ok": False, "error": "unauthorized"}
            elif request.get("generation") not in (None, self.runtime.generation):
                response = {"ok": False, "error": "execution_lost"}
            else:
                try:
                    result = await self.dispatch(request["method"], request.get("args") or {})
                    response = {"ok": True, "result": result}
                except ExecutionCapacityError as exc:
                    response = {"ok": False, "error": str(exc)}
                except (ExecutionConflict, ApprovalConflict):
                    response = {"ok": False, "error": "state_conflict"}
                except KeyError:
                    response = {"ok": False, "error": "not_found"}
                except (ValueError, TypeError):
                    response = {"ok": False, "error": "invalid_request"}
                except Exception:
                    # Never serialize arbitrary exception messages, which may
                    # contain workflow input or scoped credential values.
                    response = {"ok": False, "error": "runtime_error"}
            # A one-shot connection can disappear while the execution continues.
            # Its state and unacknowledged events belong to WorkflowRuntime.
            writer.write(encode_frame(response))
            await writer.drain()
        except (ConnectionError, TimeoutError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self._handlers.discard(task)

    async def close(self) -> None:
        await self.runtime.close()
        for task in tuple(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)


async def serve(socket_path: str, token: str, capacity: int) -> None:
    runtime = WorkflowRuntime(capacity=capacity)
    rpc = RuntimeServer(runtime, token)
    # Only stale socket removal at startup; no business payload touches disk.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(socket_path)
    server = await asyncio.start_unix_server(rpc.handle, socket_path)
    os.chmod(socket_path, 0o600)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await rpc.close()


def main() -> None:
    # Provider supplies a private pipe, which is consumed and then closed before
    # importing/running user nodes. Do not accept a token in process arguments.
    if sys.platform == "linux":
        import ctypes

        # User-code children share the sandbox uid, but must not inspect the
        # controller's memory or private socket capabilities via /proc.
        if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot protect runtime memory")
    bootstrap = json.loads(sys.stdin.readline(4096))
    sys.stdin.close()
    from ..egress_proxy import maybe_start_egress_proxy

    maybe_start_egress_proxy()
    asyncio.run(serve(sys.argv[1], bootstrap["token"], int(sys.argv[2])))


if __name__ == "__main__":
    main()
