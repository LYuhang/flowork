from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from grpc import aio as grpc_aio
from vibecanvas_api.services.sandbox.proto import sandbox_service_pb2 as pb
from vibecanvas_api.services.sandbox.proto import sandbox_service_pb2_grpc as pb_grpc
from vibecanvas_api.services.sandbox.service import (
    RemoteSandboxManager,
    SandboxDaemon,
    SandboxServiceError,
    _json_bytes,
    _json_value,
)


def test_remote_manager_recreates_grpc_stub_for_each_asyncio_run(monkeypatch):
    channels: list[object] = []

    def fake_channel(*_args, **_kwargs):
        channel = object()
        channels.append(channel)
        return channel

    monkeypatch.setattr(grpc_aio, "insecure_channel", fake_channel)
    monkeypatch.setattr(pb_grpc, "SandboxServiceStub", lambda channel: channel)
    client = RemoteSandboxManager("/tmp/loop-aware-sandbox.sock")

    async def get_stub():
        return client._get_stub()

    first = asyncio.run(get_stub())
    second = asyncio.run(get_stub())

    assert first is channels[0]
    assert second is channels[1]
    assert first is not second


class _FakeSession:
    def __init__(self, tenant_id: str, wf_id: str) -> None:
        self.tenant_id = tenant_id
        self.wf_id = wf_id
        self.run_dir = f"/tmp/{wf_id}"
        self.workflow_run_dir = self.run_dir
        self.workflow_run_id = wf_id
        self.lease = "interactive"
        self.expose_run = True
        self.closed = False
        self.controls: list[dict] = []
        self.synced_paths: list[str] = []
        self.synced_expectations: list[dict] = []

    async def read_bytes(self, path: str) -> dict:
        return {"ok": True, "data": b"rpc-bytes", "path": path}

    async def send_agent_runtime_control(self, turn_id: str, response: dict) -> None:
        self.controls.append({"turn_id": turn_id, "response": response})

    async def run_agent_runtime_stream(self, request: dict):
        yield {"type": "token", "text": request["text"]}
        yield {"type": "done"}

    async def writeback_vfs(self) -> None:
        return None

    async def sync_workspace_path(self, path: str, **expected) -> bool:
        self.synced_paths.append(path)
        self.synced_expectations.append(expected)
        return True


def test_rpc_json_round_trip_preserves_host_collections() -> None:
    payload = {
        "allow_hosts": {"httpbin.org", "openrouter.ai"},
        "immutable_hosts": frozenset({"api.openai.com"}),
    }

    decoded = _json_value(_json_bytes(payload))

    assert sorted(decoded["allow_hosts"]) == ["httpbin.org", "openrouter.ai"]
    assert decoded["immutable_hosts"] == ["api.openai.com"]


class _FakeManager:
    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str], _FakeSession] = {}
        self.shutdown_called = False
        self.prewarm_calls = 0

    async def operational_snapshot(self) -> dict[str, int]:
        return {"resident": len(self.sessions), "capacity": 8, "busy": 0,
                "resident_leases": 0, "pending_closes": 0}

    async def prewarm_base_fileops(self) -> dict[str, int | str]:
        self.prewarm_calls += 1
        return {"status": "ready", "elapsed_ms": 12}

    async def get_session(self, tenant_id: str, wf_id: str, **_kwargs) -> _FakeSession:
        self.last_acquire_options = _kwargs
        return self.sessions.setdefault((tenant_id, wf_id), _FakeSession(tenant_id, wf_id))

    async def get_loaded_session(self, tenant_id: str, wf_id: str):
        return self.sessions.get((tenant_id, wf_id))

    async def status(self, tenant_id: str, wf_id: str) -> dict:
        return {"status": "running" if (tenant_id, wf_id) in self.sessions else "idle"}

    async def sweep_idle(self) -> int:
        return 0

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest_asyncio.fixture
async def sandbox_service(tmp_path):
    daemon = object.__new__(SandboxDaemon)
    daemon.socket_path = str(tmp_path / "sandboxd.sock")
    daemon.endpoint = f"unix://{daemon.socket_path}"
    daemon.manager = _FakeManager()
    daemon.started_at = time.time()
    daemon.generation = 42
    daemon.server = None
    daemon._stop = asyncio.Event()
    daemon._reaper = None
    daemon._stopped = False
    await daemon.start()
    try:
        yield daemon
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_mount_policy_crosses_real_grpc_acquisition(sandbox_service):
    client = RemoteSandboxManager(sandbox_service.socket_path)
    try:
        await client.get_session("tenant", "schedule-test", expose_mount=False)
        assert sandbox_service.manager.last_acquire_options["expose_mount"] is False
        await client.get_session("tenant", "ordinary-chat")
        assert sandbox_service.manager.last_acquire_options["expose_mount"] is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_two_workers_reconnect_same_resident_session(sandbox_service) -> None:
    worker_a = RemoteSandboxManager(sandbox_service.socket_path)
    worker_b = RemoteSandboxManager(sandbox_service.socket_path)

    first = await worker_a.get_session("tenant", "chat", expose_runtime=True)
    resumed = await worker_b.get_loaded_session("tenant", "chat")

    assert resumed is not None
    assert first.run_dir == resumed.run_dir
    assert first.run_dir is None
    assert first.workflow_run_dir is None
    assert (await worker_b.status("tenant", "chat"))["status"] == "running"
    assert (await worker_b.health())["generation"] == 42
    await worker_a.aclose()
    await worker_b.aclose()


@pytest.mark.asyncio
async def test_binary_unary_stream_and_cross_connection_control(sandbox_service) -> None:
    client = RemoteSandboxManager(sandbox_service.socket_path)
    session = await client.get_session("tenant", "chat")

    assert (await session.read_bytes("/data/a.bin"))["data"] == b"rpc-bytes"
    assert [event async for event in session.run_agent_runtime_stream({"text": "hi"})] == [
        {"type": "token", "text": "hi"},
        {"type": "done"},
    ]
    await session.send_agent_runtime_control("turn-1", {"action": "approve"})
    owned = sandbox_service.manager.sessions[("tenant", "chat")]
    assert owned.controls == [{
        "turn_id": "turn-1", "response": {"action": "approve"},
    }]
    assert await session.sync_workspace_path("/data/diagram.json") is True
    assert owned.synced_paths == ["/data/diagram.json"]
    assert await session.sync_workspace_path("/data/download.bin", expected_sha256="a" * 64, expected_bytes=7)
    assert owned.synced_expectations == [{}, {"expected_sha256": "a" * 64, "expected_bytes": 7}]
    await client.aclose()


@pytest.mark.asyncio
async def test_base_prewarm_runs_inside_the_sandbox_service(sandbox_service) -> None:
    client = RemoteSandboxManager(sandbox_service.socket_path)

    assert await client.prewarm_base_fileops() == {
        "status": "ready",
        "elapsed_ms": 12,
    }
    assert sandbox_service.manager.prewarm_calls == 1
    assert sandbox_service.manager.sessions == {}
    await client.aclose()


@pytest.mark.asyncio
async def test_closing_runtime_stream_releases_project_activity_over_grpc(sandbox_service):
    client = RemoteSandboxManager(sandbox_service.socket_path)
    released = asyncio.Event()
    session = await client.get_session("tenant", "project")
    owned = sandbox_service.manager.sessions[("tenant", "project")]

    async def held_turn(request):
        try:
            yield {"type": "token", "text": "running"}
            await asyncio.Event().wait()
        finally:
            released.set()

    owned.run_agent_runtime_stream = held_turn
    try:
        stream = session.run_agent_runtime_stream({"turn_id": "chat-a-turn"})
        assert (await anext(stream))["text"] == "running"
        assert not released.is_set()
        await stream.aclose()
        await asyncio.wait_for(released.wait(), timeout=2)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unavailable_service_fails_closed(tmp_path) -> None:
    client = RemoteSandboxManager(str(tmp_path / "missing.sock"), connect_timeout_s=0.05)
    with pytest.raises(SandboxServiceError) as exc_info:
        await client.health()
    assert exc_info.value.code == "sandbox_unavailable"
    await client.aclose()


@pytest.mark.asyncio
async def test_one_shot_workflow_has_no_implicit_operation_deadline(
    monkeypatch,
) -> None:
    observed: dict[str, float] = {}

    class Stub:
        async def Admin(self, request, *, timeout):
            assert request.kind == "run_workflow_once"
            observed["timeout"] = timeout
            return pb.AdminResponse(payload_json=b"{}")

    client = RemoteSandboxManager("/tmp/not-connected.sock")
    monkeypatch.setattr(client, "_get_stub", lambda: Stub())
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.service.config",
        SimpleNamespace(sandbox_service_operation_timeout_s=4321.0),
    )

    assert await client._request(
        "manager.call", method="run_workflow_once", kwargs={}
    ) == {}
    assert observed["timeout"] is None


@pytest.mark.asyncio
async def test_shared_workflow_run_source_crosses_real_grpc(sandbox_service):
    from vibecanvas_api.services.sandbox.contracts import WorkflowRunSource
    from vibecanvas_api.services.sandbox.coordinator import EmbeddedSandboxClient, SandboxCoordinator
    client = RemoteSandboxManager(sandbox_service.socket_path)
    source = WorkflowRunSource(tenant_id='owner-tenant', workflow_id='shared-workflow')
    try:
        coordinator = SandboxCoordinator(EmbeddedSandboxClient(client))
        await coordinator.get_session('recipient-tenant', 'private-project', user_id='recipient',
                                      workflow_run_source=source)
        options = sandbox_service.manager.last_acquire_options
        assert options['workflow_run_source'] == source
        assert options['user_id'] == 'recipient'
        assert ('recipient-tenant', 'private-project') in sandbox_service.manager.sessions
        assert ('owner-tenant', 'shared-workflow') not in sandbox_service.manager.sessions
        await client.get_session('recipient-tenant', 'ordinary-project')
        assert sandbox_service.manager.last_acquire_options['workflow_run_source'] is None
    finally:
        await client.aclose()


def test_workflow_run_source_rejects_paths_and_unknown_fields():
    from pydantic import ValidationError
    from vibecanvas_api.services.sandbox.contracts import WorkflowRunSource
    for values in ({'tenant_id':'owner','workflow_id':'../private'},
                   {'tenant_id':'owner/another','workflow_id':'workflow'},
                   {'tenant_id':'owner','workflow_id':'workflow','directory':'/host/path'}):
        with pytest.raises(ValidationError):
            WorkflowRunSource(**values)
