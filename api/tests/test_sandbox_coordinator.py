from __future__ import annotations

import pytest

from vibecanvas_api.services.sandbox.contracts import (
    SandboxCapabilityError,
    SandboxScope,
    SandboxScopeKind,
    SandboxSpec,
)
from vibecanvas_api.services.sandbox.coordinator import (
    EmbeddedSandboxClient,
    SandboxCoordinator,
    dispose_sandbox_rpc_client,
)
from vibecanvas_api.services.sandbox import coordinator as coordinator_module
from vibecanvas_api.services.sandbox import manager as manager_module


class _Session:
    async def cancel_agent_runtime(self, operation_id: str) -> bool:
        return bool(operation_id)


class _Manager:
    def __init__(self) -> None:
        self.session = _Session()
        self.loaded = False
        self.closed = False
        self.lease = "interactive"

    async def get_session(self, tenant_id, scope_id, **kwargs):
        self.loaded = True
        self.lease = kwargs["lease"]
        return self.session

    async def get_loaded_session(self, tenant_id, scope_id):
        return self.session if self.loaded and not self.closed else None

    async def status(self, tenant_id, scope_id):
        return {"status": "running" if self.loaded and not self.closed else "idle"}

    async def close_session(self, tenant_id, scope_id):
        self.closed = True
        return {"status": "closed"}


class _RemoteManager(_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.transport_closed = False

    async def aclose(self) -> None:
        self.transport_closed = True


@pytest.mark.asyncio
async def test_embedded_client_contract_is_serializable_and_reuses_session():
    manager = _Manager()
    coordinator = SandboxCoordinator(EmbeddedSandboxClient(manager))
    spec = SandboxSpec(
        scope=SandboxScope(
            tenant_id="tenant-1",
            kind=SandboxScopeKind.PROJECT,
            scope_id="project-scope-1",
        ),
        principal_id="user-1",
    )

    ref = await coordinator.acquire(spec)
    assert ref.model_dump(mode="json") == {
        "sandbox_id": ref.sandbox_id,
        "provider": "embedded",
        "endpoint": "process://sandbox-manager",
        "generation": 1,
    }
    assert await coordinator.client.session(ref) is manager.session
    assert (await coordinator.client.inspect(ref)).status == "running"
    assert spec.scope.model_dump(mode="json")["kind"] == "project"
    assert manager.lease == "interactive"


@pytest.mark.asyncio
async def test_required_snapshot_never_silently_falls_back():
    manager = _Manager()
    coordinator = SandboxCoordinator(EmbeddedSandboxClient(manager))
    with pytest.raises(SandboxCapabilityError, match="required but unavailable"):
        await coordinator.acquire(SandboxSpec(
            scope=SandboxScope(
                tenant_id="tenant-1",
                kind=SandboxScopeKind.PROJECT,
                scope_id="project-scope-1",
            ),
            snapshot_policy="required",
        ))
    assert manager.loaded is False


@pytest.mark.asyncio
async def test_dispose_sandbox_rpc_client_closes_and_forgets_loop_bound_client(
    monkeypatch,
):
    manager = _RemoteManager()
    coordinator = SandboxCoordinator(EmbeddedSandboxClient(manager))
    monkeypatch.setattr(manager_module, "_manager", manager)
    monkeypatch.setattr(coordinator_module, "_coordinator", coordinator)

    await dispose_sandbox_rpc_client()

    assert manager.transport_closed is True
    assert manager_module.get_existing_sandbox_manager() is None
    assert coordinator_module._coordinator is None


@pytest.mark.asyncio
async def test_dispose_sandbox_rpc_client_preserves_embedded_manager(monkeypatch):
    manager = _Manager()
    coordinator = SandboxCoordinator(EmbeddedSandboxClient(manager))
    monkeypatch.setattr(manager_module, "_manager", manager)
    monkeypatch.setattr(coordinator_module, "_coordinator", coordinator)

    await dispose_sandbox_rpc_client()

    assert manager_module.get_existing_sandbox_manager() is manager
    assert coordinator_module._coordinator is coordinator
