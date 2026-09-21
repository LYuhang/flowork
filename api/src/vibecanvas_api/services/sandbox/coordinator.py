"""Sandbox coordinator and the current embedded client adapter.

The embedded adapter is valid for tests and single-worker personal deployments;
a daemon client implements the same contract without changing product layers.
"""

from __future__ import annotations

import hashlib
from typing import AsyncIterator

from vibecanvas_api.services.sandbox.contracts import (
    SandboxCapabilities,
    SandboxCapabilityError,
    SandboxClient,
    SandboxEvent,
    SandboxExecuteRequest,
    SandboxRef,
    SandboxScope,
    SandboxScopeKind,
    SandboxSpec,
    SandboxStatus,
)
from vibecanvas_api.services.sandbox.manager import (
    SandboxManager,
    clear_sandbox_manager,
    get_existing_sandbox_manager,
    get_sandbox_manager,
)
from vibecanvas_api.config import config


def _embedded_sandbox_id(scope: SandboxScope) -> str:
    digest = hashlib.sha256(
        f"{scope.tenant_id}\0{scope.kind.value}\0{scope.scope_id}".encode()
    ).hexdigest()[:32]
    return f"sbx_embedded_{digest}"


class EmbeddedSandboxClient:
    """Adapt a manager-compatible local or RPC facade to the client contract.

    The historical class name is retained for import compatibility. In normal
    deployments ``manager`` is a ``RemoteSandboxManager`` and no process-owned
    sandbox state crosses into the API process.
    """

    def __init__(self, manager: SandboxManager) -> None:
        self.manager = manager
        self._specs: dict[str, SandboxSpec] = {}

    async def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            snapshot_restore=bool(
                getattr(self.manager, "snapshot_sessions", False)
                or getattr(config, "sandbox_resident_mode", "coldboot") == "snapshot"
            ),
            hard_cancel=True,
            persistent_volume=True,
            network_policy=True,
            # Product SSE replay is durable at the Agent Run layer. The raw
            # provider stream itself is intentionally not advertised as
            # replayable until sandboxd has an event cursor/buffer contract.
            reconnectable_stream=False,
        )

    async def acquire(self, spec: SandboxSpec) -> SandboxRef:
        await self.manager.get_session(
            spec.scope.tenant_id,
            spec.scope.scope_id,
            user_id=spec.principal_id,
            expose_run=spec.expose_run,
            expose_runtime=spec.expose_runtime,
            lease=(
                "resident"
                if spec.lifecycle_policy == "resident"
                else "interactive"
            ),
        )
        sandbox_id = _embedded_sandbox_id(spec.scope)
        self._specs[sandbox_id] = spec
        provider = getattr(self.manager, "provider_name", "embedded")
        endpoint = getattr(self.manager, "endpoint", "process://sandbox-manager")
        generation = 1
        if provider != "embedded":
            health = await self.manager.health()
            generation = int(health.get("generation") or 1)
        return SandboxRef(
            sandbox_id=sandbox_id,
            provider=provider,
            endpoint=endpoint,
            generation=generation,
        )

    async def session(self, ref: SandboxRef):
        spec = self._specs.get(ref.sandbox_id)
        if spec is None:
            raise LookupError("embedded sandbox reference is not loaded")
        session = await self.manager.get_loaded_session(
            spec.scope.tenant_id, spec.scope.scope_id
        )
        if session is None:
            raise LookupError("embedded sandbox session is not loaded")
        return session

    async def loaded_session(self, scope: SandboxScope):
        return await self.manager.get_loaded_session(
            scope.tenant_id, scope.scope_id
        )

    async def execute(
        self, ref: SandboxRef, request: SandboxExecuteRequest,
    ) -> AsyncIterator[SandboxEvent]:
        raise SandboxCapabilityError(
            "generic execute is not enabled during the Embedded phase-0 cutover"
        )
        yield  # pragma: no cover - keep this an async generator

    async def cancel(self, ref: SandboxRef, operation_id: str) -> None:
        session = await self.session(ref)
        await session.cancel_agent_runtime(operation_id)

    async def inspect(self, ref: SandboxRef) -> SandboxStatus:
        spec = self._specs.get(ref.sandbox_id)
        if spec is None:
            return SandboxStatus(status="lost", ref=ref)
        payload = await self.manager.status(
            spec.scope.tenant_id, spec.scope.scope_id
        )
        return SandboxStatus(**payload, ref=ref)

    async def release(self, ref: SandboxRef) -> None:
        spec = self._specs.pop(ref.sandbox_id, None)
        if spec is not None:
            await self.manager.close_session(
                spec.scope.tenant_id, spec.scope.scope_id
            )

    async def checkpoint(self, ref: SandboxRef) -> str:
        spec = self._specs.get(ref.sandbox_id)
        if spec is None:
            raise LookupError("sandbox reference is not loaded")
        checkpoint = getattr(self.manager, "checkpoint_session", None)
        if checkpoint is None:
            raise SandboxCapabilityError("selected sandbox client cannot checkpoint")
        return await checkpoint(spec.scope.tenant_id, spec.scope.scope_id)


class SandboxCoordinator:
    """Stable product-facing owner of sandbox acquisition policy."""

    def __init__(self, client: SandboxClient) -> None:
        self.client = client

    async def acquire(self, spec: SandboxSpec) -> SandboxRef:
        capabilities = await self.client.capabilities()
        if spec.snapshot_policy == "required" and not capabilities.snapshot_restore:
            raise SandboxCapabilityError(
                "snapshot restore is required but unavailable"
            )
        return await self.client.acquire(spec)

    async def get_session(
        self,
        tenant_id: str,
        scope_id: str,
        user_id: str | None = None,
        expose_run: bool = True,
        expose_runtime: bool = False,
        lease: str = "interactive",
    ):
        """Compatibility surface used while Runtime execution is phased over."""
        spec = SandboxSpec(
            scope=SandboxScope(
                tenant_id=tenant_id,
                kind=SandboxScopeKind.CHAT,
                scope_id=scope_id,
            ),
            principal_id=user_id,
            lifecycle_policy=lease,
            expose_run=expose_run,
            expose_runtime=expose_runtime,
        )
        ref = await self.acquire(spec)
        session_resolver = getattr(self.client, "session", None)
        if session_resolver is None:
            raise SandboxCapabilityError(
                "selected client does not expose a Runtime session adapter"
            )
        return await session_resolver(ref)

    async def get_loaded_session(self, tenant_id: str, scope_id: str):
        resolver = getattr(self.client, "loaded_session", None)
        if resolver is None:
            return None
        return await resolver(SandboxScope(
            tenant_id=tenant_id,
            kind=SandboxScopeKind.CHAT,
            scope_id=scope_id,
        ))

    async def close_session(self, tenant_id: str, scope_id: str) -> dict:
        """Release a logical session through the configured service client."""
        manager = getattr(self.client, "manager", None)
        if manager is None:
            raise SandboxCapabilityError(
                "selected client does not expose session release"
            )
        return await manager.close_session(tenant_id, scope_id)

    async def set_session_lease(
        self,
        tenant_id: str,
        scope_id: str,
        lease: str,
    ) -> bool:
        """Update the lifecycle lease without bypassing the coordinator.

        Agent Runtime Turns acquire a resident lease while work is in flight,
        then return the session to the interactive idle-TTL policy.  Keeping
        this operation on the product-facing coordinator prevents callers
        from depending on whether the backing manager is local or remote.
        """
        manager = getattr(self.client, "manager", None)
        setter = getattr(manager, "set_session_lease", None)
        if setter is None:
            raise SandboxCapabilityError(
                "selected client does not expose lifecycle lease updates"
            )
        return bool(await setter(tenant_id, scope_id, lease))


_coordinator: SandboxCoordinator | None = None


def get_sandbox_coordinator() -> SandboxCoordinator:
    global _coordinator
    manager = get_sandbox_manager()
    client = getattr(_coordinator, "client", None)
    if (
        _coordinator is None
        or not isinstance(client, EmbeddedSandboxClient)
        or client.manager is not manager
    ):
        _coordinator = SandboxCoordinator(EmbeddedSandboxClient(manager))
    return _coordinator


def clear_sandbox_coordinator() -> None:
    global _coordinator
    _coordinator = None


async def dispose_sandbox_rpc_client() -> None:
    """Close and forget the loop-bound sandbox RPC client, if one exists.

    Synchronous background worker tasks commonly enter async code with a fresh
    ``asyncio.run`` loop for every task. ``grpc.aio.Channel`` objects cannot be
    reused by the next task because they remain bound to the loop that created
    them. Call this from that task's async ``finally`` block, before its loop is
    closed.

    Embedded managers own process-local sandbox sessions and intentionally do
    not expose ``aclose``; they are therefore left untouched here.
    """
    manager = get_existing_sandbox_manager()
    close = getattr(manager, "aclose", None)
    if close is None:
        return
    try:
        await close()
    finally:
        clear_sandbox_coordinator()
        clear_sandbox_manager(expected=manager)
