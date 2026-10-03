"""Persistent built-in command registry.

Commands are platform-owned capabilities activated by slash commands such as
``/workflow``. Once activated, a command stays active for the chat: tools are gated
by the active command set, and command context is injected next to the latest
activation message by ``CommandContextEdit``.
"""

from __future__ import annotations

from dataclasses import dataclass

from vibecanvas_api.agents.prompts.browser import BROWSER
from vibecanvas_api.agents.prompts.workflow import WORKFLOW
from vibecanvas_api.agents.prompts.deployment import DEPLOYMENT
from vibecanvas_api.agents.prompts.knowledge import KNOWLEDGE
from vibecanvas_api.agents.prompts.task import TASK
from vibecanvas_api.agents.prompts.skill import SKILL
from vibecanvas_api.agents.prompts.diagram import DIAGRAM
from vibecanvas_api.agents.prompts.document import DOCUMENT


COMMAND_CONTEXT_HEADER = "<command-context"

@dataclass(frozen=True)
class CommandMode:
    name: str
    trigger: str
    kind: str
    sticky: bool
    context_prompt: str
    tools: list[str]
    activation_message: str | None
    external_control: dict | None
    sidepanel_only: bool = False


COMMAND_MODES: dict[str, CommandMode] = {
    "skill": CommandMode(
        name="skill", trigger="/skill", kind="additive", sticky=True,
        context_prompt=SKILL, tools=[],
        activation_message="This is the latest /skill activation. Inspect or manage only the Skills requested by the user using the Skill CLI.",
        external_control=None,
    ),
    "task": CommandMode(
        name="task",
        trigger="/task",
        kind="additive",
        sticky=True,
        context_prompt=TASK,
        tools=[],  # Task operations use the authenticated CLI; keep the command playbook.
        activation_message=(
            "This is the latest /task activation. Use Task Center only for "
            "the durable task work requested in this message."
        ),
        external_control=None,
    ),
    "deployment": CommandMode(
        name="deployment",
        trigger="/deployment",
        kind="additive",
        sticky=True,
        context_prompt=DEPLOYMENT,
        tools=[],  # Keep guidance, not retired MCP tools.
        activation_message=(
            "This is the latest /deployment activation. Work only with the "
            "deployment resources needed by the user's request."
        ),
        external_control=None,
    ),
    "knowledge": CommandMode(
        name="knowledge",
        trigger="/knowledge",
        kind="additive",
        sticky=True,
        context_prompt=KNOWLEDGE,
        tools=[],  # Preserve command guidance without retired MCP tools.
        activation_message=(
            "This is the latest /knowledge activation. Treat Knowledge as "
            "file packages using flowork-cli knowledge, read README.md first, and publish only "
            "after validating the complete local directory."
        ),
        external_control=None,
    ),
    "workflow": CommandMode(
        name="workflow",
        trigger="/workflow",
        kind="additive",
        sticky=True,
        context_prompt=WORKFLOW,
        tools=[],
        activation_message=(
            "This is the latest /workflow activation. Treat the user's message as "
            "workflow construction or optimization work. If the user has not said "
            "what to build, ask what to build."
        ),
        external_control=None,
    ),
    "browser": CommandMode(
        name="browser",
        trigger="/browser",
        kind="additive",
        sticky=True,
        context_prompt=BROWSER,
        tools=[],  # Browser commands use the sandbox CLI and private CDP capability.
        activation_message=(
            "This is the latest /browser activation. The side-panel send action "
            "has already requested control of the visible page. Use flowork-cli "
            "browser tab-list, then snapshot --tab-id ID before choosing an action."
        ),
        external_control=None,
        sidepanel_only=True,
    ),
    "diagram": CommandMode(
        name="diagram",
        trigger="/diagram",
        kind="additive",
        sticky=True,
        context_prompt=DIAGRAM,
        tools=["render_preview"],
        activation_message=(
            "This is the latest /diagram activation. Edit native .drawio XML, "
            "use flowork-cli diagram search-shapes as needed, review and render "
            "the current file, inspect every page, then publish with render_preview."
        ),
        external_control=None,
    ),
    "document": CommandMode(
        name="document",
        trigger="/document",
        kind="additive",
        sticky=True,
        context_prompt=DOCUMENT,
        tools=["render_preview"],
        activation_message=(
            "This is the latest /document activation. Produce the requested "
            "professional native document, review its current structure and "
            "rendered pixels, then publish that exact file in Preview."
        ),
        external_control=None,
    ),
}


_TRIGGER_TO_NAME: dict[str, str] = {m.trigger: name for name, m in COMMAND_MODES.items()}


def parse_command(content: str) -> tuple[str | None, str]:
    """Resolve a leading slash command and return ``(name, stripped_content)``."""
    if not content:
        return None, content
    stripped_left = content.lstrip()
    if not stripped_left.startswith("/"):
        return None, content
    parts = stripped_left.split(None, 1)
    token = parts[0]
    name = _TRIGGER_TO_NAME.get(token)
    if name is None:
        return None, content
    return name, parts[1] if len(parts) > 1 else ""


def command_context_for(command: str) -> str:
    """Return the full persistent context injected for an active command."""
    cfg = COMMAND_MODES.get(command)
    if cfg is None:
        return ""
    parts = [
        f'{COMMAND_CONTEXT_HEADER} command="{cfg.name}">',
        cfg.context_prompt.strip(),
    ]
    if cfg.activation_message:
        parts.extend(["", "## Latest activation guidance", cfg.activation_message])
    parts.append("</command-context>")
    return "\n".join(parts)
