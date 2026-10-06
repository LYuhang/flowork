"""Deployment-neutral sandbox control-plane contracts.

These models are intentionally fully serializable.  Business code may depend
on them; provider handles, subprocesses, brokers, sockets and asyncio locks may
not cross this boundary.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, AsyncIterator, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from vibecanvas_api.services.workflow_run_source import WorkflowRunSource


class TaskRunSource(BaseModel):
    """Stable Task /run identity, separate from a worker's lease/scope.

    The authenticated Acquire scope supplies the tenant. No host path or
    execution ID is accepted as a substitute for the owning Task ID.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: UUID


class SandboxScopeKind(str, Enum):
    PROJECT = "project"
    WORKFLOW_DEBUG = "workflow_debug"
    WORKFLOW_RUN = "workflow_run"
    BACKGROUND_JOB = "background_job"


class SandboxScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    kind: SandboxScopeKind
    scope_id: str = Field(min_length=1)


class SandboxCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_restore: bool = False
    snapshot_clone: bool = False
    hard_cancel: bool = True
    dynamic_mounts: bool = False
    persistent_volume: bool = True
    network_policy: bool = True
    cpu_quota: bool = False
    memory_quota: bool = False
    pid_quota: bool = False
    reconnectable_stream: bool = False


class SandboxSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: SandboxScope
    principal_id: str | None = None
    runtime_profile: str = "agent"
    resource_profile: str = "interactive-medium"
    lifecycle_policy: str = "interactive"
    mount_specs: tuple[dict[str, Any], ...] = ()
    workflow_run_source: WorkflowRunSource | None = None
    task_run_source: TaskRunSource | None = None
    network_policy_id: str = "default"
    environment_layer_digest: str = "default"
    snapshot_policy: str = "disabled"
    expose_run: bool = True
    expose_runtime: bool = False
    expose_mount: bool = True


class SandboxRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    generation: int = Field(ge=1)


class SandboxEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox_id: str
    generation: int = Field(ge=1)
    operation_id: str
    event_seq: int = Field(ge=1)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SandboxExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class SandboxStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    ref: SandboxRef | None = None


class SandboxCapabilityError(RuntimeError):
    """The selected provider explicitly does not implement an operation."""


@runtime_checkable
class SandboxClient(Protocol):
    async def capabilities(self) -> SandboxCapabilities: ...
    async def acquire(self, spec: SandboxSpec) -> SandboxRef: ...
    async def execute(
        self, ref: SandboxRef, request: SandboxExecuteRequest,
    ) -> AsyncIterator[SandboxEvent]: ...
    async def cancel(self, ref: SandboxRef, operation_id: str) -> None: ...
    async def inspect(self, ref: SandboxRef) -> SandboxStatus: ...
    async def release(self, ref: SandboxRef) -> None: ...
    async def checkpoint(self, ref: SandboxRef) -> str: ...
