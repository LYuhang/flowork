"""gRPC sandboxd transport and process-neutral session proxies.

The daemon is the only process that owns :class:`SandboxManager` and therefore
the only process that owns gVisor handles, brokers and resident session locks.
API and Celery processes use the classes in this module as serializable gRPC
proxies. Local deployments use a Unix socket; the same protobuf contract can
be exposed over mTLS TCP by a remote Sandbox Service deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import signal
import time
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import structlog
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from vibecanvas_api.config import config
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.sandbox.proto import (
    sandbox_service_pb2 as pb,
)
from vibecanvas_api.services.sandbox.proto import (
    sandbox_service_pb2_grpc as pb_grpc,
)

logger = structlog.get_logger(__name__)

_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_STREAM_METHODS = {
    "run_agent_runtime_stream",
    "submit_workflow_stream",
}
_SESSION_METHODS = {
    "run_code",
    "run_install",
    "run_command",
    "prewarm_fileops",
    "submit_sandbox_job",
    "mcp_manifest",
    "mcp_call",
    *_STREAM_METHODS,
    "send_agent_runtime_control",
    "cancel_agent_runtime",
    "cancel_workflow_run",
    "read_file",
    "write_file",
    "read_bytes",
    "write_bytes",
    "list_dir",
    "grep",
    "edit_file",
    "sync_workspace_path",
    "writeback_vfs",
    "mirror_vfs_write",
    "mirror_vfs_delete",
    "mirror_vfs_rename",
    "acknowledge_external_vfs_commit",
    "fence_external_vfs_path",
    "drain_writeback",
    "clear_workflow_run",
    "submit_node_job",
    "execute_workflow_job",
    "close",
}
_MANAGER_METHODS = {
    "operational_snapshot",
    "prewarm_base_fileops",
    "drain_background_closes",
    "set_session_lease",
    "status",
    "close_session",
    "close_tenant",
    "close_user",
    "purge_user_storage",
    "invalidate_codex_account_sessions",
    "mirror_vfs_write",
    "mirror_vfs_delete",
    "mirror_vfs_rename",
    "sweep_idle",
    "run_mcp_probe",
    "ensure_workflow_dependencies",
    "run_workflow_once",
}


class SandboxServiceError(RuntimeError):
    """Stable error surfaced when sandboxd is unavailable or rejects an RPC."""

    def __init__(self, message: str, *, code: str = "sandbox_service_error") -> None:
        super().__init__(message)
        self.code = code


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__vibecanvas_bytes_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_encode(item) for item in sorted(value, key=repr)]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"__vibecanvas_bytes_b64__"}:
            return base64.b64decode(value["__vibecanvas_bytes_b64__"])
        return {key: _decode(item) for key, item in value.items()}
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_encode(value), separators=(",", ":"), default=str).encode()


def _json_value(value: bytes) -> Any:
    return _decode(json.loads(value or b"null"))


def _sandbox_id(tenant_id: str, wf_id: str) -> str:
    import hashlib
    digest = hashlib.sha256(f"{tenant_id}\0{wf_id}".encode()).hexdigest()[:32]
    return f"sbx_local_{digest}"


def _scope(tenant_id: str, wf_id: str, *, kind: str = "chat") -> pb.SandboxScope:
    return pb.SandboxScope(tenant_id=tenant_id, kind=kind, scope_id=wf_id)


def _descriptor(session: Any) -> pb.SessionDescriptor:
    # Host materialization paths are daemon-private implementation details.
    # Returning them is useless to a remote worker and would invite accidental
    # cross-node path coupling, so the transport exposes logical identity only.
    return pb.SessionDescriptor(
        scope=_scope(session.tenant_id, session.wf_id),
        run_dir="",
        workflow_run_dir="",
        workflow_run_id=session.workflow_run_id or "",
        lease=session.lease,
        expose_run=session.expose_run,
    )


def _metadata_from_descriptor(value: pb.SessionDescriptor, generation: int) -> dict[str, Any]:
    return {
        "tenant_id": value.scope.tenant_id,
        "wf_id": value.scope.scope_id,
        "run_dir": value.run_dir or None,
        "workflow_run_dir": value.workflow_run_dir or None,
        "workflow_run_id": value.workflow_run_id or None,
        "lease": value.lease,
        "expose_run": value.expose_run,
        "generation": generation,
        "remote": True,
    }


class RemoteSandboxSession:
    """Serializable proxy preserving the existing SandboxSession call surface."""

    def __init__(self, manager: RemoteSandboxManager, metadata: dict[str, Any]) -> None:
        self._manager = manager
        for name, value in metadata.items():
            setattr(self, name, value)
        self.closed = False
        # Workflow dependency preparation is host-side and intentionally cached
        # on the proxy. The resulting content-addressed path is visible to
        # sandboxd through the shared node filesystem.
        self._workflow_dependency_lock = asyncio.Lock()
        self._workflow_dependency_key: str | None = None
        self._workflow_dependency_pythonpath: str | None = None

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return await self._manager._request(
            "session.call",
            tenant_id=self.tenant_id,
            wf_id=self.wf_id,
            expected_generation=self.generation,
            method=method,
            args=list(args),
            kwargs=kwargs,
        )

    async def _stream(self, method: str, *args: Any, **kwargs: Any) -> AsyncIterator[dict]:
        async for item in self._manager._stream_request(
            "session.call",
            tenant_id=self.tenant_id,
            wf_id=self.wf_id,
            expected_generation=self.generation,
            method=method,
            args=list(args),
            kwargs=kwargs,
        ):
            yield item

    async def run_code(self, script: str, inputs: dict, *, timeout_s: float,
                       network: str = "egress") -> dict:
        return await self._call("run_code", script, inputs, timeout_s=timeout_s,
                                network=network)

    async def run_install(self, spec: str, *, manager: str = "pip",
                          timeout_s: float = 180) -> dict:
        return await self._call("run_install", spec, manager=manager, timeout_s=timeout_s)

    async def run_command(self, command: str, *, timeout_s: float = 60) -> dict:
        return await self._call("run_command", command, timeout_s=timeout_s)

    async def prewarm_fileops(self) -> None:
        await self._call("prewarm_fileops")

    async def submit_sandbox_job(self, job: dict, *, timeout: float = 600.0) -> dict:
        return await self._call("submit_sandbox_job", job, timeout=timeout)

    async def mcp_manifest(self, server: dict, *, timeout_s: float = 30.0) -> dict:
        return await self._call("mcp_manifest", server, timeout_s=timeout_s)

    async def mcp_call(self, server: dict, *, tool_name: str, arguments: dict,
                       timeout_s: float = 120.0) -> dict:
        return await self._call("mcp_call", server, tool_name=tool_name,
                                arguments=arguments, timeout_s=timeout_s)

    async def run_agent_runtime_stream(self, request: dict) -> AsyncIterator[dict]:
        async for item in self._stream("run_agent_runtime_stream", request):
            yield item

    async def send_agent_runtime_control(self, turn_id: str, response: dict) -> None:
        await self._call("send_agent_runtime_control", turn_id, response)

    async def cancel_agent_runtime(self, turn_id: str) -> bool:
        return bool(await self._call("cancel_agent_runtime", turn_id))

    async def submit_workflow_stream(self, **kwargs: Any) -> AsyncIterator[dict]:
        async for item in self._stream("submit_workflow_stream", **kwargs):
            yield item

    async def clear_workflow_run(self, workflow_run_id: str | None = None) -> None:
        await self._call("clear_workflow_run", workflow_run_id)

    async def submit_node_job(self, **kwargs: Any) -> dict:
        return await self._call("submit_node_job", **kwargs)

    async def execute_workflow_job(self, **kwargs: Any) -> dict:
        return await self._call("execute_workflow_job", **kwargs)

    async def cancel_workflow_run(self, **kwargs: Any) -> None:
        await self._call("cancel_workflow_run", **kwargs)

    async def read_file(self, path: str) -> dict:
        return await self._call("read_file", path)

    async def write_file(self, path: str, content: str) -> dict:
        return await self._call("write_file", path, content)

    async def read_bytes(self, path: str) -> dict:
        return await self._call("read_bytes", path)

    async def write_bytes(self, path: str, data: bytes) -> dict:
        return await self._call("write_bytes", path, data)

    async def list_dir(self, path: str) -> dict:
        return await self._call("list_dir", path)

    async def grep(self, pattern: str, path: str, glob: str = "", context: int = 0) -> dict:
        return await self._call("grep", pattern, path, glob, context)

    async def edit_file(self, path: str, old: str, new: str,
                        replace_all: bool = False) -> dict:
        return await self._call("edit_file", path, old, new, replace_all)

    async def sync_workspace_path(self, path: str) -> bool:
        return bool(await self._call("sync_workspace_path", path))

    async def writeback_vfs(self) -> None:
        await self._call("writeback_vfs")

    async def mirror_vfs_write(self, path: str, data: bytes) -> bool:
        return bool(await self._call("mirror_vfs_write", path, data))

    async def mirror_vfs_delete(self, path: str) -> bool:
        return bool(await self._call("mirror_vfs_delete", path))

    async def mirror_vfs_rename(self, old_path: str, new_path: str) -> bool:
        return bool(await self._call("mirror_vfs_rename", old_path, new_path))

    async def acknowledge_external_vfs_commit(self, path: str, data: bytes) -> bool:
        return bool(await self._call("acknowledge_external_vfs_commit", path, data))

    async def fence_external_vfs_path(self, path: str) -> bool:
        return bool(await self._call("fence_external_vfs_path", path))

    def schedule_writeback(self) -> None:
        asyncio.create_task(self.writeback_vfs(), name=f"sandboxd-writeback-{self.wf_id}")

    async def drain_writeback(self) -> None:
        await self._call("drain_writeback")

    async def close(self) -> None:
        self.closed = True
        await self._manager.close_session(self.tenant_id, self.wf_id)


class RemoteSandboxManager:
    """Manager-compatible facade used by every non-sandboxd process."""

    provider_name = "local-sandbox-service"

    def __init__(self, endpoint: str, *, connect_timeout_s: float = 5.0) -> None:
        self.endpoint = endpoint if "://" in endpoint else f"unix://{endpoint}"
        self.socket_path = (
            self.endpoint.removeprefix("unix://")
            if self.endpoint.startswith("unix://") else ""
        )
        self.connect_timeout_s = connect_timeout_s
        self.max_resident = config.sandbox_max_resident
        self.idle_ttl_s = config.sandbox_idle_ttl_s
        self._channel: grpc.aio.Channel | None = None
        self._stub: pb_grpc.SandboxServiceStub | None = None
        self._channel_loop: asyncio.AbstractEventLoop | None = None

    def _get_stub(self) -> pb_grpc.SandboxServiceStub:
        # grpc.aio channels are bound to the event loop that created them.
        # Celery's synchronous task wrappers use asyncio.run(), so a worker
        # process receives a fresh loop for each task. Never reuse a stub from a
        # previous (usually already closed) loop; API processes keep reusing the
        # same channel because their running loop is stable.
        loop = asyncio.get_running_loop()
        if self._channel_loop is not None and self._channel_loop is not loop:
            self._channel = None
            self._stub = None
            self._channel_loop = None
        if self._stub is None:
            options = (
                ("grpc.max_send_message_length", _MAX_MESSAGE_BYTES),
                ("grpc.max_receive_message_length", _MAX_MESSAGE_BYTES),
                ("grpc.enable_retries", 1),
            )
            if self.endpoint.startswith("unix://"):
                self._channel = grpc.aio.insecure_channel(
                    f"unix:{self.socket_path}", options=options,
                )
            elif self.endpoint.startswith("grpcs://"):
                required = (
                    config.sandbox_service_ca_file,
                    config.sandbox_service_cert_file,
                    config.sandbox_service_key_file,
                )
                if not all(required):
                    raise SandboxServiceError(
                        "remote sandbox service requires CA, client certificate and key",
                        code="sandbox_mtls_required",
                    )
                credentials = grpc.ssl_channel_credentials(
                    root_certificates=Path(required[0]).read_bytes(),
                    private_key=Path(required[2]).read_bytes(),
                    certificate_chain=Path(required[1]).read_bytes(),
                )
                self._channel = grpc.aio.secure_channel(
                    self.endpoint.removeprefix("grpcs://"), credentials,
                    options=options,
                )
            else:
                raise SandboxServiceError(
                    "sandbox endpoint must use unix:// or grpcs://",
                    code="sandbox_endpoint_invalid",
                )
            self._stub = pb_grpc.SandboxServiceStub(self._channel)
            self._channel_loop = loop
        return self._stub

    @staticmethod
    def _translate_error(exc: grpc.aio.AioRpcError) -> SandboxServiceError:
        metadata = dict(exc.trailing_metadata() or ())
        code = metadata.get("sandbox-error-code")
        if not code:
            code = (
                "sandbox_unavailable"
                if exc.code() == grpc.StatusCode.UNAVAILABLE
                else "sandbox_deadline_exceeded"
                if exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED
                else "sandbox_operation_failed"
            )
        return SandboxServiceError(exc.details() or str(exc), code=code)

    @staticmethod
    def _operation_timeout(method: str, kwargs: dict[str, Any]) -> float:
        declared = kwargs.get("timeout_s", kwargs.get("timeout", 0))
        if isinstance(declared, (int, float)) and declared > 0:
            return max(float(declared) + 30.0, 60.0)
        if method in {"ensure_workflow_dependencies", "run_workflow_once"}:
            return max(float(config.sandbox_service_operation_timeout_s), 60.0)
        return 600.0

    async def _request(self, op: str, **params: Any) -> Any:
        stub = self._get_stub()
        try:
            if op == "health":
                response = await stub.Stats(
                    pb.StatsRequest(), timeout=self.connect_timeout_s, wait_for_ready=True,
                )
                return {
                    "status": response.status, "pid": response.pid,
                    "generation": response.generation,
                    "uptime_s": response.uptime_seconds,
                    "resident": response.resident, "capacity": response.capacity,
                    "busy": response.busy,
                    "resident_leases": response.resident_leases,
                    "pending_closes": response.pending_closes,
                }
            if op == "session.acquire":
                response = await stub.Acquire(pb.AcquireRequest(
                    scope=_scope(params["tenant_id"], params["wf_id"]),
                    principal_id=params.get("user_id") or "",
                    lifecycle_policy=params.get("lease") or "interactive",
                    expose_run=bool(params.get("expose_run", True)),
                    expose_runtime=bool(params.get("expose_runtime", False)),
                ), timeout=max(self.connect_timeout_s, 120.0), wait_for_ready=True)
                return _metadata_from_descriptor(
                    response.session, int(response.ref.generation)
                )
            if op == "session.loaded":
                response = await stub.Connect(pb.ConnectRequest(
                    scope=_scope(params["tenant_id"], params["wf_id"]),
                ), timeout=self.connect_timeout_s, wait_for_ready=True)
                if not response.found:
                    return None
                return _metadata_from_descriptor(
                    response.session, int(response.ref.generation)
                )
            if op == "manager.call":
                response = await stub.Admin(pb.AdminRequest(
                    kind=str(params["method"]),
                    payload_json=_json_bytes({
                        "args": params.get("args") or [],
                        "kwargs": params.get("kwargs") or {},
                    }),
                ), timeout=self._operation_timeout(
                    str(params["method"]), params.get("kwargs") or {}
                ))
                return _json_value(response.payload_json)
            if op != "session.call":
                raise SandboxServiceError("unknown sandbox service operation")
            method = str(params["method"])
            payload = _json_bytes({
                "args": params.get("args") or [],
                "kwargs": params.get("kwargs") or {},
            })
            if method in {
                "send_agent_runtime_control", "cancel_agent_runtime",
                "cancel_workflow_run",
            }:
                response = await stub.Control(pb.ControlRequest(
                    scope=_scope(params["tenant_id"], params["wf_id"]),
                    expected_generation=int(params.get("expected_generation") or 0),
                    operation_id=str((params.get("args") or [""])[0]),
                    action=method,
                    payload_json=payload,
                ), timeout=30.0)
                return _json_value(response.payload_json)
            response = await stub.Invoke(pb.InvokeRequest(
                scope=_scope(params["tenant_id"], params["wf_id"]),
                expected_generation=int(params.get("expected_generation") or 0),
                operation_id=str((params.get("kwargs") or {}).get("operation_id") or ""),
                kind=method,
                payload_json=payload,
            ), timeout=self._operation_timeout(method, params.get("kwargs") or {}))
            return _json_value(response.payload_json)
        except grpc.aio.AioRpcError as exc:
            translated = self._translate_error(exc)
            if op == "health" and exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                translated.code = "sandbox_unavailable"
            raise translated from exc

    async def _stream_request(self, op: str, **params: Any) -> AsyncIterator[Any]:
        if op != "session.call":
            raise SandboxServiceError("unknown sandbox stream operation")
        stub = self._get_stub()
        method = str(params["method"])
        try:
            call = stub.Execute(pb.ExecuteRequest(
                scope=_scope(params["tenant_id"], params["wf_id"]),
                expected_generation=int(params.get("expected_generation") or 0),
                operation_id=str(
                    (params.get("args") or [{}])[0].get("turn_id")
                    if params.get("args") and isinstance(params["args"][0], dict)
                    else ""
                ),
                kind=method,
                payload_json=_json_bytes({
                    "args": params.get("args") or [],
                    "kwargs": params.get("kwargs") or {},
                }),
            ), timeout=24 * 60 * 60)
            async for event in call:
                yield _json_value(event.payload_json)
        except grpc.aio.AioRpcError as exc:
            raise self._translate_error(exc) from exc

    async def health(self) -> dict[str, Any]:
        return await self._request("health")

    async def operational_snapshot(self) -> dict[str, int]:
        return await self._request("manager.call", method="operational_snapshot")

    async def prewarm_base_fileops(self) -> dict[str, Any]:
        return await self._manager_call("prewarm_base_fileops")

    async def get_session(self, tenant_id: str, wf_id: str, user_id: str | None = None,
                          expose_run: bool = True, expose_runtime: bool = False,
                          lease: str = "interactive") -> RemoteSandboxSession:
        metadata = await self._request(
            "session.acquire", tenant_id=tenant_id, wf_id=wf_id, user_id=user_id,
            expose_run=expose_run, expose_runtime=expose_runtime, lease=lease,
        )
        return RemoteSandboxSession(self, metadata)

    async def get_loaded_session(self, tenant_id: str,
                                 wf_id: str) -> RemoteSandboxSession | None:
        metadata = await self._request("session.loaded", tenant_id=tenant_id, wf_id=wf_id)
        return RemoteSandboxSession(self, metadata) if metadata else None

    async def _manager_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return await self._request(
            "manager.call", method=method, args=list(args), kwargs=kwargs,
        )

    async def set_session_lease(self, tenant_id: str, wf_id: str, lease: str) -> bool:
        return bool(await self._manager_call("set_session_lease", tenant_id, wf_id, lease))

    async def status(self, tenant_id: str, wf_id: str) -> dict:
        return await self._manager_call("status", tenant_id, wf_id)

    async def close_session(self, tenant_id: str, wf_id: str) -> dict:
        return await self._manager_call("close_session", tenant_id, wf_id)

    async def checkpoint_session(self, tenant_id: str, wf_id: str) -> str:
        stub = self._get_stub()
        try:
            response = await stub.Checkpoint(
                pb.CheckpointRequest(
                    scope=_scope(tenant_id, wf_id),
                    expected_generation=0,
                    policy="idle",
                ),
                timeout=float(config.sandbox_snapshot_checkpoint_timeout_s) + 30.0,
            )
            return response.snapshot_id
        except grpc.aio.AioRpcError as exc:
            raise self._translate_error(exc) from exc

    async def close_tenant(self, tenant_id: str, *, reason: str = "tenant_purge") -> int:
        return int(await self._manager_call("close_tenant", tenant_id, reason=reason))

    async def close_user(self, user_id: str, *, reason: str = "account_purge") -> int:
        return int(await self._manager_call("close_user", user_id, reason=reason))

    async def purge_user_storage(
        self,
        user_id: str,
        tenant_ids: list[str],
        personal_tenant_id: str,
    ) -> bool:
        return bool(
            await self._manager_call(
                "purge_user_storage",
                user_id,
                tenant_ids,
                personal_tenant_id,
            )
        )

    async def invalidate_codex_account_sessions(self, tenant_id: str, user_id: str) -> int:
        return int(await self._manager_call(
            "invalidate_codex_account_sessions", tenant_id, user_id,
        ))

    async def mirror_vfs_write(self, tenant_id: str, wf_id: str, path: str,
                               data: bytes) -> bool:
        return bool(await self._manager_call(
            "mirror_vfs_write", tenant_id, wf_id, path, data,
        ))

    async def mirror_vfs_delete(
        self, tenant_id: str, wf_id: str, path: str,
    ) -> bool:
        return bool(await self._manager_call(
            "mirror_vfs_delete", tenant_id, wf_id, path,
        ))

    async def mirror_vfs_rename(
        self, tenant_id: str, wf_id: str, old_path: str, new_path: str,
    ) -> bool:
        return bool(await self._manager_call(
            "mirror_vfs_rename", tenant_id, wf_id, old_path, new_path,
        ))

    async def sweep_idle(self) -> int:
        # sandboxd owns its own idle reaper. Kept for compatibility with older
        # app lifespans and administrative callers.
        return 0

    async def run_mcp_probe(
        self,
        tenant_id: str,
        request: dict,
        *,
        timeout: float,
        allow_hosts: list[str],
    ) -> dict:
        return await self._manager_call(
            "run_mcp_probe",
            tenant_id,
            request,
            timeout=timeout,
            allow_hosts=allow_hosts,
        )

    async def run_workflow_once(self, **kwargs: Any) -> dict:
        return await self._manager_call("run_workflow_once", **kwargs)

    async def ensure_workflow_dependencies(self, requirements: str) -> dict:
        return await self._manager_call(
            "ensure_workflow_dependencies", requirements,
        )

    async def drain_background_closes(self) -> None:
        await self._manager_call("drain_background_closes")

    async def shutdown(self) -> None:
        # API/worker restart must never tear down the independently supervised
        # sandbox service or its resident sessions.
        return None

    async def aclose(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None


class _SandboxGrpcService(pb_grpc.SandboxServiceServicer):
    def __init__(self, daemon: SandboxDaemon) -> None:
        self.daemon = daemon

    def _ref(self, scope: pb.SandboxScope) -> pb.SandboxRef:
        return pb.SandboxRef(
            sandbox_id=_sandbox_id(scope.tenant_id, scope.scope_id),
            provider="gvisor-local",
            endpoint=getattr(
                self.daemon, "endpoint", f"unix://{self.daemon.socket_path}"
            ),
            generation=self.daemon.generation,
        )

    async def _session(self, scope: pb.SandboxScope, expected_generation: int,
                       context: grpc.aio.ServicerContext):
        if expected_generation not in {0, self.daemon.generation}:
            await context.abort(
                grpc.StatusCode.ABORTED,
                "sandbox generation changed; reconnect before retrying",
                trailing_metadata=(("sandbox-error-code", "sandbox_generation_stale"),),
            )
        session = await self.daemon.manager.get_loaded_session(
            scope.tenant_id, scope.scope_id
        )
        if session is None:
            await context.abort(
                grpc.StatusCode.NOT_FOUND,
                "sandbox session is not loaded",
                trailing_metadata=(("sandbox-error-code", "sandbox_session_lost"),),
            )
        return session

    async def _fail(self, context: grpc.aio.ServicerContext, exc: Exception) -> None:
        logger.error(
            "sandbox_service_request_failed", error=str(exc),
            traceback=traceback.format_exc(),
        )
        code = getattr(exc, "code", "sandbox_operation_failed")
        status = grpc.StatusCode.INTERNAL
        if code in {"invalid_method", "invalid_operation"}:
            status = grpc.StatusCode.INVALID_ARGUMENT
        elif "capacity" in str(exc).lower():
            status = grpc.StatusCode.RESOURCE_EXHAUSTED
            code = "sandbox_capacity_exhausted"
        await context.abort(
            status, str(exc), trailing_metadata=(("sandbox-error-code", code),)
        )

    async def Stats(self, request: pb.StatsRequest,
                    context: grpc.aio.ServicerContext) -> pb.StatsResponse:
        del request, context
        stats = await self.daemon.manager.operational_snapshot()
        return pb.StatsResponse(
            status="ok", pid=os.getpid(), generation=self.daemon.generation,
            uptime_seconds=max(0.0, time.time() - self.daemon.started_at),
            resident=stats["resident"], capacity=stats["capacity"],
            busy=stats["busy"], resident_leases=stats["resident_leases"],
            pending_closes=stats["pending_closes"],
        )

    async def Acquire(self, request: pb.AcquireRequest,
                      context: grpc.aio.ServicerContext) -> pb.AcquireResponse:
        try:
            session = await self.daemon.manager.get_session(
                request.scope.tenant_id,
                request.scope.scope_id,
                user_id=request.principal_id or None,
                expose_run=request.expose_run,
                expose_runtime=request.expose_runtime,
                lease=request.lifecycle_policy or "interactive",
            )
            return pb.AcquireResponse(
                ref=self._ref(request.scope), session=_descriptor(session)
            )
        except Exception as exc:
            await self._fail(context, exc)
            raise

    async def Connect(self, request: pb.ConnectRequest,
                      context: grpc.aio.ServicerContext) -> pb.ConnectResponse:
        if request.expected_generation not in {0, self.daemon.generation}:
            await context.abort(
                grpc.StatusCode.ABORTED,
                "sandbox generation changed",
                trailing_metadata=(("sandbox-error-code", "sandbox_generation_stale"),),
            )
        session = await self.daemon.manager.get_loaded_session(
            request.scope.tenant_id, request.scope.scope_id
        )
        if session is None:
            return pb.ConnectResponse(found=False)
        return pb.ConnectResponse(
            found=True, ref=self._ref(request.scope), session=_descriptor(session)
        )

    async def Inspect(self, request: pb.InspectRequest,
                      context: grpc.aio.ServicerContext) -> pb.InspectResponse:
        try:
            status = await self.daemon.manager.status(
                request.scope.tenant_id, request.scope.scope_id
            )
            return pb.InspectResponse(
                status=str(status.get("status") or "unknown"),
                ref=self._ref(request.scope), details_json=_json_bytes(status),
            )
        except Exception as exc:
            await self._fail(context, exc)
            raise

    async def Execute(self, request: pb.ExecuteRequest,
                      context: grpc.aio.ServicerContext):
        try:
            if request.kind not in _STREAM_METHODS:
                raise SandboxServiceError(
                    "stream operation is not allowed", code="invalid_method"
                )
            session = await self._session(
                request.scope, request.expected_generation, context
            )
            call = _json_value(request.payload_json) or {}
            stream = getattr(session, request.kind)(
                *(call.get("args") or []), **(call.get("kwargs") or {})
            )
            seq = 0
            async for item in stream:
                seq += 1
                yield pb.SandboxEvent(
                    sandbox_id=_sandbox_id(
                        request.scope.tenant_id, request.scope.scope_id
                    ),
                    generation=self.daemon.generation,
                    operation_id=request.operation_id,
                    event_seq=seq,
                    type=str(item.get("type") or item.get("kind") or "event"),
                    payload_json=_json_bytes(item),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(context, exc)

    async def Invoke(self, request: pb.InvokeRequest,
                     context: grpc.aio.ServicerContext) -> pb.InvokeResponse:
        try:
            if request.kind not in _SESSION_METHODS or request.kind in _STREAM_METHODS:
                raise SandboxServiceError(
                    "session operation is not allowed", code="invalid_method"
                )
            session = await self._session(
                request.scope, request.expected_generation, context
            )
            call = _json_value(request.payload_json) or {}
            result = getattr(session, request.kind)(
                *(call.get("args") or []), **(call.get("kwargs") or {})
            )
            if asyncio.iscoroutine(result):
                result = await result
            return pb.InvokeResponse(payload_json=_json_bytes(result))
        except Exception as exc:
            await self._fail(context, exc)
            raise

    async def Control(self, request: pb.ControlRequest,
                      context: grpc.aio.ServicerContext) -> pb.ControlResponse:
        try:
            allowed = {
                "send_agent_runtime_control", "cancel_agent_runtime",
                "cancel_workflow_run",
            }
            if request.action not in allowed:
                raise SandboxServiceError(
                    "control operation is not allowed", code="invalid_method"
                )
            session = await self._session(
                request.scope, request.expected_generation, context
            )
            call = _json_value(request.payload_json) or {}
            result = getattr(session, request.action)(
                *(call.get("args") or []), **(call.get("kwargs") or {})
            )
            if asyncio.iscoroutine(result):
                result = await result
            return pb.ControlResponse(
                accepted=result is not False, payload_json=_json_bytes(result)
            )
        except Exception as exc:
            await self._fail(context, exc)
            raise

    async def Release(self, request: pb.ReleaseRequest,
                      context: grpc.aio.ServicerContext) -> pb.InspectResponse:
        try:
            if request.expected_generation not in {0, self.daemon.generation}:
                await context.abort(
                    grpc.StatusCode.ABORTED, "sandbox generation changed",
                    trailing_metadata=((
                        "sandbox-error-code", "sandbox_generation_stale",
                    ),),
                )
            status = await self.daemon.manager.close_session(
                request.scope.tenant_id, request.scope.scope_id
            )
            return pb.InspectResponse(
                status=str(status.get("status") or "closed"),
                ref=self._ref(request.scope), details_json=_json_bytes(status),
            )
        except Exception as exc:
            await self._fail(context, exc)
            raise

    async def Checkpoint(self, request: pb.CheckpointRequest,
                         context: grpc.aio.ServicerContext) -> pb.CheckpointResponse:
        try:
            if request.expected_generation not in {0, self.daemon.generation}:
                await context.abort(
                    grpc.StatusCode.ABORTED,
                    "sandbox generation changed",
                    trailing_metadata=((
                        "sandbox-error-code", "sandbox_generation_stale",
                    ),),
                )
            if request.policy not in {"", "idle", "manual"}:
                raise SandboxServiceError(
                    "unsupported checkpoint policy", code="invalid_operation"
                )
            snapshot_id = await self.daemon.manager.checkpoint_session(
                request.scope.tenant_id, request.scope.scope_id
            )
            return pb.CheckpointResponse(snapshot_id=snapshot_id)
        except Exception as exc:
            await self._fail(context, exc)
            raise

    async def Admin(self, request: pb.AdminRequest,
                    context: grpc.aio.ServicerContext) -> pb.AdminResponse:
        try:
            if request.kind not in _MANAGER_METHODS:
                raise SandboxServiceError(
                    "manager operation is not allowed", code="invalid_method"
                )
            call = _json_value(request.payload_json) or {}
            result = await getattr(self.daemon.manager, request.kind)(
                *(call.get("args") or []), **(call.get("kwargs") or {})
            )
            return pb.AdminResponse(payload_json=_json_bytes(result))
        except Exception as exc:
            await self._fail(context, exc)
            raise


class SandboxDaemon:
    """Own the process-local SandboxManager and expose its gRPC contract."""

    def __init__(
        self,
        endpoint: str,
        *,
        tls_cert_file: str = "",
        tls_key_file: str = "",
        client_ca_file: str = "",
    ) -> None:
        from vibecanvas_api.services.sandbox.manager import SandboxManager

        self.endpoint = endpoint if "://" in endpoint else f"unix://{endpoint}"
        self.socket_path = (
            self.endpoint.removeprefix("unix://")
            if self.endpoint.startswith("unix://") else ""
        )
        self.tls_cert_file = tls_cert_file
        self.tls_key_file = tls_key_file
        self.client_ca_file = client_ca_file
        self.manager = SandboxManager(
            max_resident=config.sandbox_max_resident,
            idle_ttl_s=config.sandbox_idle_ttl_s,
        )
        self.started_at = time.time()
        self.generation = max(1, int(self.started_at * 1000))
        self.server: grpc.aio.Server | None = None
        self._health: health.aio.HealthServicer | None = None
        self._stop = asyncio.Event()
        self._reaper: asyncio.Task | None = None
        self._stopped = False

    async def _reap(self) -> None:
        # A fixed, operator-controlled observation cadence keeps elapsed-idle
        # accounting independent from the configured TTL length.
        interval = float(config.sandbox_activity_poll_interval_s)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    await self.manager.sweep_idle()
                except Exception:
                    logger.exception("sandbox_activity_maintenance_failed")

    async def start(self) -> None:
        # Rootful sandboxd is the authority that can migrate ciphertext written
        # by older owner-only containers.  Prepare the shared object store
        # before advertising health so unprivileged API/worker processes can
        # immediately preview retained run files after a restart.
        await asyncio.to_thread(get_object_store)
        path = Path(self.socket_path) if self.socket_path else None
        if path is not None:
            directory_mode = int(config.sandbox_service_socket_dir_mode)
            socket_gid = int(config.sandbox_service_socket_gid)
            path.parent.mkdir(parents=True, exist_ok=True, mode=directory_mode)
            if socket_gid >= 0:
                os.chown(path.parent, -1, socket_gid)
            os.chmod(path.parent, directory_mode)
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        self.server = grpc.aio.server(options=(
            ("grpc.max_send_message_length", _MAX_MESSAGE_BYTES),
            ("grpc.max_receive_message_length", _MAX_MESSAGE_BYTES),
        ))
        pb_grpc.add_SandboxServiceServicer_to_server(
            _SandboxGrpcService(self), self.server
        )
        self._health = health.aio.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(self._health, self.server)
        if self.endpoint.startswith("unix://"):
            bound = self.server.add_insecure_port(f"unix:{self.socket_path}")
        elif self.endpoint.startswith("grpcs://"):
            required = (
                self.tls_cert_file, self.tls_key_file, self.client_ca_file,
            )
            if not all(required):
                raise RuntimeError(
                    "remote sandboxd requires server certificate, key and client CA"
                )
            credentials = grpc.ssl_server_credentials(
                ((Path(self.tls_key_file).read_bytes(),
                  Path(self.tls_cert_file).read_bytes()),),
                root_certificates=Path(self.client_ca_file).read_bytes(),
                require_client_auth=True,
            )
            bound = self.server.add_secure_port(
                self.endpoint.removeprefix("grpcs://"), credentials,
            )
        else:
            raise RuntimeError("sandboxd endpoint must use unix:// or grpcs://")
        if bound == 0:
            raise RuntimeError(f"could not bind sandbox service endpoint {self.endpoint}")
        await self.server.start()
        if self.socket_path:
            if config.sandbox_service_socket_gid >= 0:
                os.chown(self.socket_path, -1, config.sandbox_service_socket_gid)
            os.chmod(self.socket_path, config.sandbox_service_socket_mode)
        await self._health.set(
            "", health_pb2.HealthCheckResponse.SERVING
        )
        await self._health.set(
            "vibecanvas.sandbox.v1.SandboxService",
            health_pb2.HealthCheckResponse.SERVING,
        )
        self._reaper = asyncio.create_task(self._reap(), name="sandboxd-idle-reaper")
        logger.info("sandbox_service_ready", endpoint=self.endpoint,
                    generation=self.generation)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        if self._health is not None:
            await self._health.enter_graceful_shutdown()
        if self.server is not None:
            await self.server.stop(grace=10.0)
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(BaseException):
                await self._reaper
        await self.manager.shutdown()
        if self.socket_path:
            with contextlib.suppress(FileNotFoundError):
                Path(self.socket_path).unlink()

    async def serve(self) -> None:
        await self.start()
        assert self.server is not None
        await self._stop.wait()


async def _run_daemon(
    endpoint: str,
    *,
    tls_cert_file: str = "",
    tls_key_file: str = "",
    client_ca_file: str = "",
) -> None:
    daemon = SandboxDaemon(
        endpoint,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        client_ca_file=client_ca_file,
    )
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, daemon._stop.set)
    try:
        await daemon.serve()
    finally:
        await daemon.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Flowork local sandbox service")
    parser.add_argument("--socket", default=config.sandbox_service_socket)
    parser.add_argument("--listen", default="")
    parser.add_argument("--tls-cert", default="")
    parser.add_argument("--tls-key", default="")
    parser.add_argument("--client-ca", default="")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--prewarm", action="store_true")
    args = parser.parse_args()
    endpoint = f"grpcs://{args.listen}" if args.listen else f"unix://{args.socket}"
    if args.health:
        async def probe() -> None:
            result = await RemoteSandboxManager(endpoint).health()
            print(json.dumps(result, ensure_ascii=False))
        asyncio.run(probe())
        return
    if args.prewarm:
        async def prewarm() -> None:
            result = await RemoteSandboxManager(endpoint).prewarm_base_fileops()
            print(json.dumps(result, ensure_ascii=False))
        asyncio.run(prewarm())
        return
    asyncio.run(_run_daemon(
        endpoint,
        tls_cert_file=args.tls_cert,
        tls_key_file=args.tls_key,
        client_ca_file=args.client_ca,
    ))


if __name__ == "__main__":  # pragma: no cover
    main()
