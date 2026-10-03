"""Backend-owned Agent Runtime instruction resolution."""

from __future__ import annotations

from collections.abc import Iterable
import json

from vibecanvas_api.agents.commands import command_context_for
from vibecanvas_api.services.agent_runtime.protocol import RuntimeInstruction


COMMAND_CONTEXT_VERSION = 32


def command_instructions_for_modes(
    active_modes: Iterable[str],
    *,
    activated_this_turn: Iterable[str] = (),
    active_diagram: dict | None = None,
) -> list[RuntimeInstruction]:
    """Resolve command names into immutable, Runtime-neutral prompt blocks."""

    active = sorted(set(active_modes))
    activated = set(activated_this_turn)
    unknown_activations = activated.difference(active)
    if unknown_activations:
        raise ValueError(
            "activated commands are not active: "
            + ", ".join(sorted(unknown_activations))
        )

    instructions: list[RuntimeInstruction] = []
    for name in active:
        content = command_context_for(name)
        if not content:
            raise ValueError(f"active command has no context definition: {name}")
        if name == "diagram" and active_diagram:
            file_ref = active_diagram.get("file_ref")
            if isinstance(file_ref, dict):
                source_path = str(file_ref.get("path") or "")
                active_context = {
                    "active_diagram": active_diagram,
                    "editable_source_path": source_path,
                }
                content = (
                    f"{content}\n\n## Active Diagram Context\n"
                    f"{json.dumps(active_context, ensure_ascii=False, sort_keys=True)}"
                )
        instructions.append(
            RuntimeInstruction(
                instruction_id=(
                    f"command:{name}:v{COMMAND_CONTEXT_VERSION}"
                ),
                kind="command_context",
                scope="chat",
                name=name,
                version=COMMAND_CONTEXT_VERSION,
                content=content,
                activated_this_turn=name in activated,
            )
        )
    return instructions
