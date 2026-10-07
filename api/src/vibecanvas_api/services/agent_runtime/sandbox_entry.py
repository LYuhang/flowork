"""Codex Runtime entrypoint executed inside the user sandbox."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress


def _append_extra_python_paths() -> None:
    for path in os.environ.get("VC_SANDBOX_PYTHON_PATHS", "").split(os.pathsep):
        if path and path not in sys.path:
            sys.path.append(path)


_append_extra_python_paths()

from vibecanvas_engine.sandbox_bus import (  # noqa: E402
    MSG_RUNTIME_CONTROL,
    MSG_RUNTIME_RESULT,
    MSG_RUNTIME_ERROR,
    MSG_RUNTIME_REQUEST,
    connect_bus,
)

from vibecanvas_api.services.agent_runtime.mcp_hub import SandboxMcpHub  # noqa: E402
from vibecanvas_api.services.agent_runtime.protocol import (  # noqa: E402
    RuntimeTurnRequest,
    RuntimeType,
)


class ChatRuntime:
    """Independent Codex CLI resources inside the shared Project sandbox."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.active = 0
        self.client = None
        self.startup_key = None
        self.gateways = {}
        self.threads = {}
        self.hub = None
        self.adapter = None

    async def close(self):
        if self.client is not None:
            with suppress(Exception):
                await self.client.close()
            self.client = None
        for gateway in reversed(list(self.gateways.values())):
            with suppress(Exception):
                await gateway.close()
        self.gateways.clear()
        self.threads.clear()
        if self.hub is not None:
            with suppress(Exception):
                await self.hub.close()
        self.hub = self.adapter = None


_chat_runtimes: dict[str, ChatRuntime] = {}


class TurnChannel:
    def __init__(self, channel, turn_id: str):
        self.channel = channel
        self.turn_id = turn_id
        self.incoming = asyncio.Queue()

    async def send(self, message):
        await self.channel.send({**message, "turn_id": self.turn_id})

    async def recv(self):
        return await self.incoming.get()


def _preload_runtime(runtime_type: str) -> None:
    """Import the credential-free Codex adapter before snapshotting."""
    if runtime_type != RuntimeType.CODEX.value:
        raise ValueError(f"unsupported Runtime bootstrap type: {runtime_type}")
    from vibecanvas_api.services.agent_runtime import codex as _codex  # noqa: F401


async def _wait_for_bus_socket(socket_path: str) -> None:
    while not os.path.exists(socket_path):
        await asyncio.sleep(0.02)


async def _run(channel, request: RuntimeTurnRequest) -> None:
    if request.runtime_type != RuntimeType.CODEX:
        raise ValueError(f"unsupported runtime type: {request.runtime_type.value}")
    from vibecanvas_api.services.agent_runtime.codex import (
        codex_app_server_startup_key, create_codex_app_server, run_codex_turn,
    )
    from vibecanvas_api.services.agent_runtime.mcp_hub_adapter import SandboxMcpRuntimeAdapter

    runtime = _chat_runtimes.setdefault(request.chat_id, ChatRuntime())
    runtime.active += 1
    try:
        async with runtime.lock:
            try:
                key = codex_app_server_startup_key(request)
                if runtime.client is not None and runtime.startup_key != key:
                    await runtime.close()
                if runtime.client is None:
                    runtime.client = create_codex_app_server(request)
                    runtime.startup_key = key

                async def unconfigured_gateway(*_args, **_kwargs):
                    raise RuntimeError("MCP Host Gateway is not active")

                if runtime.adapter is None:
                    runtime.adapter = SandboxMcpRuntimeAdapter(unconfigured_gateway)
                    runtime.hub = SandboxMcpHub(runtime.adapter)
                await run_codex_turn(
                    channel, request, client=runtime.client, close_client=False,
                    mcp_hub=runtime.hub, mcp_adapter=runtime.adapter,
                    hub_gateway_registry=runtime.gateways, resident_threads=runtime.threads,
                )
            except BaseException:
                # Closing A's CLI cannot terminate B's CLI or replace its callbacks.
                await runtime.close()
                raise
    finally:
        runtime.active -= 1
    # Keep at most two idle CLIs warm, plus any currently active Chats.
    idle = [key for key, value in _chat_runtimes.items() if value.active == 0]
    for key in idle[:-2]:
        retired = _chat_runtimes.pop(key)
        await retired.close()


async def serve_turns(channel) -> None:
    turns: dict[str, tuple[TurnChannel, asyncio.Task]] = {}

    async def execute(turn, request):
        try:
            await _run(turn, request)
        except asyncio.CancelledError:
            await turn.send({"type": MSG_RUNTIME_RESULT, "cancelled": True})
        except Exception as exc:
            await turn.send({"type": MSG_RUNTIME_ERROR,
                             "error": {"code": "runtime_adapter_failed", "message": str(exc)}})
        finally:
            turns.pop(request.turn_id, None)

    try:
        while True:
            envelope = await channel.recv()
            if envelope is None:
                return
            kind = envelope.get("type")
            if kind == "runtime_execution_status":
                from vibecanvas_api.services.sandbox.local_activity import execution_alive
                try:
                    alive = execution_alive(envelope.get("run_id", ""))
                except (OSError, ValueError, TypeError, AttributeError):
                    alive = None
                await channel.send({"type": kind, "turn_id": envelope.get("turn_id"),
                                    "run_id": envelope.get("run_id"), "alive": alive})
            elif kind == MSG_RUNTIME_REQUEST:
                raw = envelope.get("request") or {}
                turn_id = str(raw.get("turn_id") or "")
                try:
                    request = RuntimeTurnRequest.model_validate(raw)
                    if request.turn_id in turns:
                        raise ValueError("duplicate runtime turn")
                except Exception as exc:
                    await channel.send({"type": MSG_RUNTIME_ERROR, "turn_id": turn_id,
                                        "error": {"message": str(exc)}})
                    continue
                turn = TurnChannel(channel, request.turn_id)
                task = asyncio.create_task(execute(turn, request))
                turns[request.turn_id] = (turn, task)
            elif kind == MSG_RUNTIME_CONTROL:
                response = envelope.get("response") or {}
                target = turns.get(str(envelope.get("turn_id") or response.get("turn_id") or ""))
                if target is not None:
                    turn, task = target
                    if response.get("action") == "cancel":
                        # Terminate only this Chat's isolated CLI. This also
                        # handles cancellation during startup or a stuck tool.
                        if not task.cancelling():
                            task.cancel()
                    else:
                        turn.incoming.put_nowait(envelope)
            else:
                raise ValueError("expected runtime_request or runtime_control")
            # In-memory test channels may not yield; also start queued Turns
            # before processing a disconnect/cancel immediately after request.
            await asyncio.sleep(0)
    finally:
        tasks = [task for _, task in turns.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> int:
    socket_path = os.environ.get("VC_BUS_SOCK", "")
    if not socket_path:
        raise RuntimeError("VC_BUS_SOCK is required")
    runtime_type = os.environ.get("VC_AGENT_RUNTIME_TYPE", "")
    if runtime_type:
        _preload_runtime(runtime_type)
    ready_path = os.environ.get("VC_AGENT_RUNTIME_BOOTSTRAP_READY", "")
    if ready_path:
        descriptor = os.open(
            ready_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        os.close(descriptor)
        await _wait_for_bus_socket(socket_path)

    from vibecanvas_engine.egress_proxy import maybe_start_egress_proxy

    egress_proxy = maybe_start_egress_proxy()
    channel = await connect_bus(socket_path)
    try:
        await serve_turns(channel)
        return 0
    finally:
        _ = egress_proxy
        for runtime in list(_chat_runtimes.values()):
            await runtime.close()
        _chat_runtimes.clear()
        await channel.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
