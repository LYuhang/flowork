"""Runtime-neutral contracts for Agent SDKs hosted inside a sandbox.

Keep package import deliberately light.  ``sandbox_entry`` must import the
wire protocol before it can connect to the host bus; eagerly importing the
host orchestrator here would pull SQLAlchemy, the sandbox manager and workflow
engine into every fresh Runtime process before that connection.
Heavy host adapters remain available through lazy compatibility attributes.
"""

from typing import Any

from .protocol import (
    RUNTIME_PROTOCOL_VERSION,
    RuntimeControlResponse,
    RuntimeCapabilities,
    RuntimeCapabilitiesRequest,
    RuntimeCommandContext,
    RuntimeEvent,
    RuntimeInstruction,
    RuntimeModelOption,
    RuntimeOpenRequest,
    RuntimeRequestCorrelation,
    RuntimeSession,
    RuntimeSkill,
    RuntimeTurnRequest,
    RuntimeType,
    RuntimeReasoningEffortOption,
)

__all__ = [
    "RUNTIME_PROTOCOL_VERSION",
    "RuntimeControlResponse",
    "RuntimeCapabilities",
    "RuntimeCapabilitiesRequest",
    "RuntimeCommandContext",
    "RuntimeEvent",
    "RuntimeInstruction",
    "RuntimeModelOption",
    "RuntimeOpenRequest",
    "RuntimeRequestCorrelation",
    "RuntimeSession",
    "RuntimeSkill",
    "RuntimeTurnRequest",
    "RuntimeType",
    "RuntimeReasoningEffortOption",
    "CodexSandboxRuntime",
    "AgentRuntimeOrchestrator",
    "private_runtime_root",
]


def __getattr__(name: str) -> Any:
    if name == "CodexSandboxRuntime":
        from .codex_runtime import CodexSandboxRuntime

        return CodexSandboxRuntime
    if name in {"AgentRuntimeOrchestrator", "private_runtime_root"}:
        from .orchestrator import AgentRuntimeOrchestrator, private_runtime_root

        return {
            "AgentRuntimeOrchestrator": AgentRuntimeOrchestrator,
            "private_runtime_root": private_runtime_root,
        }[name]
    raise AttributeError(name)
