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
    MSG_RUNTIME_ERROR,
    MSG_RUNTIME_REQUEST,
    connect_bus,
)

from vibecanvas_api.services.agent_runtime.mcp_hub import SandboxMcpHub  # noqa: E402
from vibecanvas_api.services.agent_runtime.protocol import (  # noqa: E402
    RuntimeTurnRequest,
    RuntimeType,
)


_codex_client = None
_codex_client_startup_key: str | None = None
_codex_hub_gateways = {}
_codex_threads = {}
_active_mcp_hub: SandboxMcpHub | None = None
_active_mcp_adapter = None


def _preload_runtime(runtime_type: str) -> None:
    """Import the credential-free Codex adapter before snapshotting."""
    if runtime_type != RuntimeType.CODEX.value:
        raise ValueError(f"unsupported Runtime bootstrap type: {runtime_type}")
    from vibecanvas_api.services.agent_runtime import codex as _codex  # noqa: F401


async def _wait_for_bus_socket(socket_path: str) -> None:
    while not os.path.exists(socket_path):
        await asyncio.sleep(0.02)


async def _close_codex_runtime_resources() -> None:
    global _codex_client, _codex_client_startup_key
    global _codex_hub_gateways, _codex_threads
    client = _codex_client
    hub_gateways = _codex_hub_gateways
    _codex_client = None
    _codex_client_startup_key = None
    _codex_hub_gateways = {}
    _codex_threads = {}

    async def finish(coroutine) -> None:
        task = asyncio.create_task(coroutine)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task

    if client is not None:
        with suppress(Exception):
            await finish(client.close())
    for gateway in reversed(list(hub_gateways.values())):
        with suppress(Exception):
            await finish(gateway.close())


async def _run(channel, request: RuntimeTurnRequest) -> None:
    if request.runtime_type != RuntimeType.CODEX:
        raise ValueError(f"unsupported runtime type: {request.runtime_type.value}")

    from vibecanvas_api.services.agent_runtime.codex import (
        codex_app_server_startup_key,
        create_codex_app_server,
        run_codex_turn,
    )
    from vibecanvas_api.services.agent_runtime.mcp_hub_adapter import (
        SandboxMcpRuntimeAdapter,
    )

    global _active_mcp_adapter, _active_mcp_hub
    global _codex_client, _codex_client_startup_key
    global _codex_hub_gateways, _codex_threads
    startup_key = codex_app_server_startup_key(request)
    if _codex_client is not None and _codex_client_startup_key != startup_key:
        await _close_codex_runtime_resources()
    if _codex_client is None:
        _codex_client = create_codex_app_server(request)
        _codex_client_startup_key = startup_key

    async def unconfigured_gateway(*_args, **_kwargs):
        raise RuntimeError("MCP Host Gateway is not active")

    if _active_mcp_adapter is None:
        _active_mcp_adapter = SandboxMcpRuntimeAdapter(unconfigured_gateway)
    if _active_mcp_hub is None:
        _active_mcp_hub = SandboxMcpHub(_active_mcp_adapter)
    await run_codex_turn(
        channel,
        request,
        client=_codex_client,
        close_client=False,
        mcp_hub=_active_mcp_hub,
        mcp_adapter=_active_mcp_adapter,
        hub_gateway_registry=_codex_hub_gateways,
        resident_threads=_codex_threads,
    )


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
        while True:
            envelope = await channel.recv()
            if envelope is None:
                return 0
            if envelope.get("type") != MSG_RUNTIME_REQUEST:
                raise ValueError("expected runtime_request")
            try:
                request = RuntimeTurnRequest.model_validate(
                    envelope.get("request") or {}
                )
                await _run(channel, request)
            except Exception as exc:
                await channel.send({
                    "type": MSG_RUNTIME_ERROR,
                    "error": {
                        "code": "runtime_adapter_failed",
                        "message": str(exc),
                    },
                })
    except Exception as exc:
        with suppress(Exception):
            await channel.send({
                "type": MSG_RUNTIME_ERROR,
                "error": {
                    "code": "runtime_adapter_failed",
                    "message": str(exc),
                },
            })
        return 1
    finally:
        _ = egress_proxy
        global _active_mcp_hub, _active_mcp_adapter
        if _active_mcp_hub is not None:
            await _active_mcp_hub.close()
            _active_mcp_hub = None
            _active_mcp_adapter = None
        await _close_codex_runtime_resources()
        await channel.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
