"""Node-type prompt/tool formatters.

The BUILD command context carries compact specs for core graph nodes and a
lightweight catalog for extended nodes. Full node definitions remain available
on demand through ``flowork-cli workflow get-spec``.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from vibecanvas_engine.register import node_registry


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _format_json_example(value: Any, *, indent: int = 0, inline_limit: int = 96) -> str:
    compact = _compact_json(value)
    if len(compact) <= inline_limit and "\n" not in compact:
        return compact

    pad = " " * indent
    child_pad = " " * (indent + 2)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        items = list(value.items())
        for idx, (key, child) in enumerate(items):
            comma = "," if idx < len(items) - 1 else ""
            key_json = json.dumps(key, ensure_ascii=False)
            child_json = _format_json_example(child, indent=indent + 2, inline_limit=inline_limit)
            if "\n" in child_json:
                lines.append(f"{child_pad}{key_json}: {child_json}{comma}")
            else:
                lines.append(f"{child_pad}{key_json}: {child_json}{comma}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)

    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for idx, child in enumerate(value):
            comma = "," if idx < len(value) - 1 else ""
            child_json = _format_json_example(child, indent=indent + 2, inline_limit=inline_limit)
            lines.append(f"{child_pad}{child_json}{comma}")
        lines.append(f"{pad}]")
        return "\n".join(lines)

    return compact


def _format_examples(examples: list) -> str:
    parts = []
    for ex in examples:
        nd_json = _format_json_example(ex["node_dict"])
        parts.append(f"**{ex['scenario']}:**\n```json\n{nd_json}\n```")
    return "\n\n".join(parts)


def _format_optional_section(title: str, body: str) -> str:
    if not body:
        return ""
    return f"\n{title}:\n{body}\n"


# Node types kept in the engine registry for back-compat but withheld from the
# agent's node catalog (deprecated for general use). Currently empty — kept so
# re-deprecating a type later is a one-line set edit.
_HIDDEN_AGENT_NODE_TYPES: set[str] = set()
_CORE_BUILD_NODE_TYPES: tuple[str, ...] = (
    "StartNode",
    "EndNode",
    "CodeNode",
    "PromptNode",
    "SubAgentNode",
    "ConditionNode",
    "HumanApprovalNode",
    "ParallelStartNode",
    "ParallelEndNode",
    "LoopBeginNode",
    "LoopEndNode",
)


def _runtime_vars(runtime_vars: dict | None = None) -> dict:
    if runtime_vars is not None:
        return runtime_vars
    try:
        from vibecanvas_api.enums import build_runtime_vars
        return build_runtime_vars()
    except Exception:
        return {}


def _substitute(value: Any, runtime_vars: dict) -> Any:
    if isinstance(value, str):
        out = value
        for key, val in runtime_vars.items():
            out = out.replace(f"{{{key}}}", val)
        return out
    if isinstance(value, list):
        return [_substitute(v, runtime_vars) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, runtime_vars) for k, v in value.items()}
    return value


def _visible_node_items():
    for name, node_cls in node_registry._module_dict.items():
        if name in _HIDDEN_AGENT_NODE_TYPES:
            continue
        spec = getattr(node_cls, "AGENT_SPEC", None)
        if spec:
            yield name, node_cls, spec


def available_node_types() -> list[str]:
    """Node types the agent may use."""
    return [name for name, _node_cls, _spec in _visible_node_items()]


def build_node_spec(node_type: str, runtime_vars: dict | None = None) -> dict:
    """Return the canonical node definition used by the CLI and prompt catalog."""

    node_cls = node_registry._module_dict.get(node_type)
    if not node_cls or node_type in _HIDDEN_AGENT_NODE_TYPES:
        raise KeyError(node_type)
    spec = getattr(node_cls, "AGENT_SPEC", None)
    if not spec:
        raise KeyError(node_type)
    vars_ = _runtime_vars(runtime_vars)
    return {
        "node_type": node_type,
        "summary": _substitute(spec.get("summary", ""), vars_),
        "when_to_use": _substitute(spec.get("when_to_use", ""), vars_),
        "when_not_to_use": _substitute(spec.get("when_not_to_use", ""), vars_),
        "constraints": _substitute(spec.get("constraints", []), vars_),
        "config_guide": _substitute(spec.get("config_guide", {}), vars_),
        "config_schema": _substitute(deepcopy(getattr(node_cls, "CONFIG_SCHEMA", {})), vars_),
        "examples": _substitute(spec.get("examples", []), vars_),
    }


def format_node_spec(spec: dict) -> str:
    """Agent-facing text for a single node spec."""

    constraints_text = "\n".join(f"- {c}" for c in spec.get("constraints", []))
    config_guide = spec.get("config_guide") or {}
    config_guide_text = (
        "\n".join(f"- `{k}`: {v}" for k, v in config_guide.items())
        if config_guide else ""
    )
    schema_json = json.dumps(spec.get("config_schema") or {}, ensure_ascii=False, indent=2)
    examples_text = _format_examples(spec.get("examples", [])) or "No examples."
    return f"""# Node spec: {spec.get('node_type')}

Summary: {spec.get('summary', '')}

When to use: {spec.get('when_to_use', '')}

When not to use: {spec.get('when_not_to_use', '')}
{_format_optional_section("Constraints", constraints_text)}{_format_optional_section("Config fields", config_guide_text)}

CONFIG_SCHEMA:
```json
{schema_json}
```

Examples:
{examples_text}"""


def core_build_node_types() -> tuple[str, ...]:
    """Node types whose compact specs are embedded in the BUILD context."""
    visible = set(available_node_types())
    return tuple(node_type for node_type in _CORE_BUILD_NODE_TYPES if node_type in visible)


def _first_example(spec: dict) -> str:
    examples = spec.get("examples") or []
    if not examples:
        return "None."
    ex = examples[0]
    scenario = ex.get("scenario") or "Minimal example"
    node_dict = ex.get("node_dict") or {}
    return f"{scenario}:\n```json\n{_format_json_example(node_dict, inline_limit=128)}\n```"


def format_compact_node_spec(spec: dict) -> str:
    """Compact embedded spec for core build nodes."""
    constraints = spec.get("constraints", [])
    constraints_text = "\n".join(f"- {c}" for c in constraints)
    config_guide = spec.get("config_guide") or {}
    config_guide_text = (
        "\n".join(f"- `{k}`: {v}" for k, v in config_guide.items())
        if config_guide else ""
    )
    schema_json = json.dumps(spec.get("config_schema") or {}, ensure_ascii=False, separators=(",", ":"))
    return f"""#### `{spec.get('node_type')}`
Summary: {spec.get('summary', '')}
Use: {spec.get('when_to_use', '')}
Avoid: {spec.get('when_not_to_use', '')}
{_format_optional_section("Key constraints", constraints_text)}{_format_optional_section("Config guide", config_guide_text)}
CONFIG_SCHEMA:
```json
{schema_json}
```
Compact example:
{_first_example(spec)}"""


def format_node_catalog_for_prompt(runtime_vars: dict | None = None) -> str:
    """BUILD-context node section: compact core specs + extended catalog.

    Core flow nodes are embedded because they are frequent and structurally
    error-prone. Extended/specialized nodes remain one-line catalog entries and
    should be loaded through flowork-cli workflow get-spec before use.
    """

    core = set(core_build_node_types())
    core_parts = []
    for node_type in core_build_node_types():
        spec = build_node_spec(node_type, runtime_vars)
        core_parts.append(format_compact_node_spec(spec))

    extended_parts = []
    for node_type in available_node_types():
        if node_type in core:
            continue
        spec = build_node_spec(node_type, runtime_vars)
        use_text = " ".join(str(spec.get("when_to_use", "")).split()).rstrip(".")
        use_suffix = f" {use_text}." if use_text else ""
        extended_parts.append(
            f"- `{node_type}`: {spec.get('summary', '')}{use_suffix}"
        )

    sections = [
        "#### Core node specs\n"
        "These core node types are embedded because most workflows use them and their graph constraints are easy to get wrong.\n\n"
        + "\n\n".join(core_parts)
    ]
    if extended_parts:
        sections.append(
            "#### Extended node catalog\n"
            "For these specialized node types, use this catalog for orientation and run `flowork-cli workflow get-spec --type <NodeType>` for exact schema, constraints, and examples before use.\n\n"
            + "\n".join(extended_parts)
        )
    return "\n\n".join(sections)
