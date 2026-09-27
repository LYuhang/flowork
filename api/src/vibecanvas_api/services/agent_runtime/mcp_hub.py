"""Sandbox-owned MCP lifecycle and execution-context state machine.

The Hub core is transport-independent. Runtime adapters and the future local
Streamable HTTP endpoint use this single registry; Host credentials and
authorization services remain behind the adapter boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from .mcp_runtime_protocol import (
    McpDesiredServer,
    McpDesiredState,
    McpExecutionContext,
    McpHubStatus,
    McpReconcileResult,
    McpServerStatus,
)


logger = structlog.get_logger(__name__)


class McpHubError(RuntimeError):
    """Base error surfaced by the sandbox-local Hub."""


class McpHubInactiveError(McpHubError):
    """A model-visible call was attempted without a live execution context."""


class McpHubReconcileError(McpHubError):
    """A required server failed before the desired registry could be swapped."""

    def __init__(self, message: str, result: McpReconcileResult) -> None:
        super().__init__(message)
        self.result = result


class McpHubAdapter(Protocol):
    async def start(self, server: McpDesiredServer) -> tuple[str, ...]: ...

    async def stop(self, server: McpDesiredServer) -> None: ...

    async def call(
        self,
        server: McpDesiredServer,
        tool_name: str,
        arguments: dict[str, Any],
        execution_context: McpExecutionContext,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class _ActiveServer:
    desired: McpDesiredServer
    tools: tuple[str, ...]
    state: str = "ready"
    error_code: str | None = None


def _status(runtime: _ActiveServer) -> McpServerStatus:
    return McpServerStatus(
        id=runtime.desired.id,
        name=runtime.desired.name,
        source=runtime.desired.source,
        state=runtime.state,
        tool_count=len(runtime.tools),
        configuration_revision=runtime.desired.configuration_revision,
        last_error_code=runtime.error_code,
    )


class SandboxMcpHub:
    """Own one Chat Runtime's warm MCP registry inside the sandbox process."""

    def __init__(self, adapter: McpHubAdapter) -> None:
        self._adapter = adapter
        self._lock = asyncio.Lock()
        self._drained = asyncio.Condition(self._lock)
        self._desired: McpDesiredState | None = None
        self._servers: dict[str, _ActiveServer] = {}
        self._execution_context: McpExecutionContext | None = None
        self._execution_state = "inactive"
        self._active_calls = 0

    async def reconcile(self, desired: McpDesiredState) -> McpReconcileResult:
        """Atomically prepare and swap a new desired registry.

        Unchanged server revisions retain their live sessions. Required server
        failures abort the swap; optional failures are committed as degraded so
        unrelated tools remain available.
        """
        async with self._lock:
            if self._desired is not None:
                identity_fields = (
                    "organization_id",
                    "user_id",
                    "chat_id",
                    "runtime_session_id",
                    "sandbox_id",
                    "sandbox_generation",
                )
                for field in identity_fields:
                    if getattr(self._desired, field) != getattr(desired, field):
                        raise McpHubError(
                            f"MCP desired state changed immutable {field}"
                        )
                if self._desired.revision_key == desired.revision_key:
                    return self._result(
                        desired_revision=desired.chat_mcp_config_revision,
                        changed=(),
                        removed=(),
                    )
            current = dict(self._servers)
            desired_by_id = {server.id: server for server in desired.servers}
            changed = [
                server
                for server in desired.servers
                if server.id not in current
                or current[server.id].desired.configuration_revision
                != server.configuration_revision
            ]
            removed = [
                runtime
                for server_id, runtime in current.items()
                if server_id not in desired_by_id
                or any(server.id == server_id for server in changed)
            ]

            prepared: dict[str, _ActiveServer] = {}
            for server in changed:
                try:
                    tools = await self._adapter.start(server)
                    prepared[server.id] = _ActiveServer(
                        desired=server,
                        tools=tuple(tools),
                    )
                except Exception as exc:
                    logger.warning(
                        "sandbox_mcp_server_start_failed",
                        server_id=server.id,
                        server_name=server.name,
                        source=server.source,
                        required=server.required,
                        error_type=type(exc).__name__,
                        error=str(exc)[:500],
                    )
                    prepared[server.id] = _ActiveServer(
                        desired=server,
                        tools=(),
                        state="failed" if server.required else "degraded",
                        error_code=type(exc).__name__,
                    )
                    if server.required:
                        for runtime in prepared.values():
                            if runtime.state == "ready":
                                await self._adapter.stop(runtime.desired)
                        failed_statuses = [
                            _status(prepared_server)
                            for prepared_server in prepared.values()
                        ]
                        result = McpReconcileResult(
                            desired_revision=desired.chat_mcp_config_revision,
                            applied_revision=(
                                self._desired.chat_mcp_config_revision
                                if self._desired is not None
                                else 0
                            ),
                            required_ready=False,
                            changed_server_ids=[item.id for item in changed],
                            removed_server_ids=[item.desired.id for item in removed],
                            servers=failed_statuses,
                        )
                        raise McpHubReconcileError(
                            f"required MCP server {server.name!r} failed to start",
                            result,
                        ) from exc

            next_servers = {
                server_id: runtime
                for server_id, runtime in current.items()
                if server_id in desired_by_id
                and not any(server.id == server_id for server in changed)
            }
            next_servers.update(prepared)
            self._servers = next_servers
            self._desired = desired
            # A new desired revision invalidates any old Turn projection. The
            # Host must activate an execution context for the accepted revision.
            self._execution_context = None
            self._execution_state = "inactive"

            for runtime in removed:
                try:
                    await self._adapter.stop(runtime.desired)
                except Exception:
                    # The registry is already fenced. A failed best-effort drain
                    # cannot make the removed server callable again.
                    pass

            return self._result(
                desired_revision=desired.chat_mcp_config_revision,
                changed=(server.id for server in changed),
                removed=(runtime.desired.id for runtime in removed),
            )

    async def activate(self, context: McpExecutionContext) -> None:
        async with self._lock:
            desired = self._desired
            if desired is None:
                raise McpHubError("MCP Hub has no reconciled desired state")
            expected = {
                "organization_id": desired.organization_id,
                "user_id": desired.user_id,
                "chat_id": desired.chat_id,
                "runtime_session_id": desired.runtime_session_id,
                "sandbox_generation": desired.sandbox_generation,
                "selected_mcp_revision": desired.chat_mcp_config_revision,
            }
            for field, value in expected.items():
                if getattr(context, field) != value:
                    raise McpHubError(
                        f"MCP execution context does not match {field}"
                    )
            if context.is_expired():
                raise McpHubError("MCP execution context is expired")
            desired_platform = {
                server.name
                for server in desired.servers
                if server.source == "platform"
            }
            if not set(context.active_platform_capabilities).issubset(
                desired_platform | set(desired.private_platform_capabilities)
            ):
                raise McpHubError(
                    "MCP execution context expands the desired Platform set"
                )
            for runtime in self._servers.values():
                if runtime.state != "ready":
                    continue
                activate = getattr(self._adapter, "activate", None)
                if activate is not None:
                    await activate(runtime.desired, context)
            self._execution_context = context
            self._execution_state = "active"

    async def deactivate(self) -> None:
        async with self._drained:
            self._execution_state = "draining"
            self._execution_context = None
            while self._active_calls:
                await self._drained.wait()
            for runtime in self._servers.values():
                deactivate = getattr(self._adapter, "deactivate", None)
                if deactivate is not None:
                    try:
                        await deactivate(runtime.desired)
                    except Exception:
                        pass
            self._execution_state = "inactive"

    async def call(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        async with self._lock:
            context = self._execution_context
            if (
                self._execution_state != "active"
                or context is None
                or context.is_expired()
            ):
                raise McpHubInactiveError(
                    "MCP tool calls require an active execution context"
                )
            runtime = next(
                (
                    item
                    for item in self._servers.values()
                    if item.desired.name == server_name
                ),
                None,
            )
            if runtime is None or runtime.state != "ready":
                raise McpHubError(f"MCP server {server_name!r} is not ready")
            if (
                runtime.desired.source == "platform"
                and runtime.desired.name
                not in context.active_platform_capabilities
            ):
                raise McpHubError(
                    f"Platform MCP {server_name!r} is inactive for this Turn"
                )
            if tool_name not in runtime.tools:
                raise McpHubError(
                    f"MCP tool {server_name}.{tool_name} is not in the manifest"
                )
            self._active_calls += 1
        try:
            return await self._adapter.call(
                runtime.desired,
                tool_name,
                dict(arguments),
                context,
            )
        finally:
            async with self._drained:
                self._active_calls -= 1
                if self._active_calls == 0:
                    self._drained.notify_all()

    async def status(self) -> McpHubStatus:
        async with self._lock:
            desired = self._desired
            if desired is None:
                raise McpHubError("MCP Hub has not been bootstrapped")
            return McpHubStatus(
                sandbox_generation=desired.sandbox_generation,
                config_revision=desired.chat_mcp_config_revision,
                execution_state=self._execution_state,
                active_call_count=self._active_calls,
                servers=[
                    _status(self._servers[server_id])
                    for server_id in sorted(self._servers)
                ],
            )

    async def close(self) -> None:
        await self.deactivate()
        async with self._lock:
            runtimes = list(self._servers.values())
            self._servers = {}
            self._desired = None
        for runtime in runtimes:
            try:
                await self._adapter.stop(runtime.desired)
            except Exception:
                pass

    def _result(
        self,
        *,
        desired_revision: int,
        changed: Any,
        removed: Any,
    ) -> McpReconcileResult:
        applied = (
            self._desired.chat_mcp_config_revision
            if self._desired is not None
            else 0
        )
        statuses = [
            _status(self._servers[server_id])
            for server_id in sorted(self._servers)
        ]
        return McpReconcileResult(
            desired_revision=desired_revision,
            applied_revision=applied,
            required_ready=all(
                status.state == "ready"
                for status in statuses
                if next(
                    server.desired.required
                    for server in self._servers.values()
                    if server.desired.id == status.id
                )
            ),
            changed_server_ids=list(changed),
            removed_server_ids=list(removed),
            servers=statuses,
        )


__all__ = [
    "McpHubAdapter",
    "McpHubError",
    "McpHubInactiveError",
    "McpHubReconcileError",
    "SandboxMcpHub",
]
