"""Installed sandbox Runtime adapters and their public descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    id: str
    label: str
    description: str
    adapter_module: str
    adapter_class: str

    def public_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
        }


RUNTIME_REGISTRY: dict[str, RuntimeDescriptor] = {
    "codex": RuntimeDescriptor(
        id="codex",
        label="Codex",
        description="Coding and general-purpose agent runtime.",
        adapter_module="vibecanvas_api.services.agent_runtime.codex_runtime",
        adapter_class="CodexSandboxRuntime",
    ),
}

AVAILABLE_RUNTIME_TYPES = frozenset(RUNTIME_REGISTRY)


def create_runtime_adapter(runtime_type: str, sandbox: Any):
    """Instantiate one registered host-side sandbox adapter lazily."""
    try:
        descriptor = RUNTIME_REGISTRY[runtime_type]
    except KeyError as exc:
        raise RuntimeError(f"runtime adapter unavailable: {runtime_type}") from exc
    module = import_module(descriptor.adapter_module)
    adapter = getattr(module, descriptor.adapter_class)
    return adapter(sandbox)


def runtime_options(runtime_types: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        RUNTIME_REGISTRY[runtime_type].public_dict()
        for runtime_type in runtime_types
        if runtime_type in RUNTIME_REGISTRY
    ]


__all__ = [
    "AVAILABLE_RUNTIME_TYPES",
    "RUNTIME_REGISTRY",
    "RuntimeDescriptor",
    "create_runtime_adapter",
    "runtime_options",
]
