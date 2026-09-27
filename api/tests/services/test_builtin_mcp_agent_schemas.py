"""Keep built-in MCP inputs simple enough for heterogeneous Agent models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vibecanvas_api.services.platform_mcp.invocation import platform_mcp_tool_manifest


PLATFORM_SERVERS = ("interactive",)
SCALAR_TYPES = {"boolean", "integer", "number", "string"}
INTERNAL_ARGUMENT_NAMES = {
    "capability",
    "chat_id",
    "organization_id",
    "runtime",
    "tenant_id",
    "turn_id",
    "user_id",
}


def _assert_agent_friendly_schema(*, tool_name: str, schema: dict[str, Any]) -> None:
    """Reject shapes that commonly produce invalid calls across providers."""
    assert schema.get("type") == "object", tool_name
    assert len(schema.get("required", [])) <= 4, tool_name
    properties = schema.get("properties", {})
    assert not (set(properties) & INTERNAL_ARGUMENT_NAMES), tool_name

    def inspect(value: Any, path: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                inspect(item, f"{path}[{index}]")
            return
        if not isinstance(value, dict):
            return
        assert "oneOf" not in value, f"{tool_name}: {path} uses oneOf"
        assert "allOf" not in value, f"{tool_name}: {path} uses allOf"
        assert "discriminator" not in value, f"{tool_name}: {path} uses discriminator"
        if "anyOf" in value:
            branches = value["anyOf"]
            branch_types = {branch.get("type") for branch in branches}
            assert branch_types <= (SCALAR_TYPES | {"array", "object", "null"}), (
                f"{tool_name}: {path} has a structured anyOf"
            )
            assert "null" in branch_types and len(branch_types) == 2, (
                f"{tool_name}: {path} has a non-nullable union"
            )
            for branch in branches:
                if branch.get("type") == "array":
                    assert branch.get("items", {}).get("type") in SCALAR_TYPES, (
                        f"{tool_name}: {path} has a structured array branch"
                    )
                if branch.get("type") == "object":
                    # A workflow input preset is naturally a free-form JSON
                    # mapping. It may remain one object leaf, but must not grow
                    # a model-facing nested protocol of its own.
                    assert branch.get("additionalProperties") is True, (
                        f"{tool_name}: {path} has a structured object branch"
                    )
                    assert "properties" not in branch, (
                        f"{tool_name}: {path} has nested object fields"
                    )
        for key, item in value.items():
            inspect(item, f"{path}.{key}")

    inspect(schema, "$")
    for name, prop in properties.items():
        prop_type = prop.get("type")
        assert prop_type != "object", f"{tool_name}: {name} is a nested object"
        if prop_type == "array":
            if tool_name == "interactive/render_choices" and name == "options":
                # User-approved row format: a single flat array of labeled
                # choices, not a general nested form-building language.
                assert prop["items"] == {"$ref": "#/$defs/ChoiceOption"}
                option = schema["$defs"]["ChoiceOption"]
                assert set(option["properties"]) == {"id", "label", "description"}
                assert all(p["type"] == "string" for p in option["properties"].values())
                assert option["additionalProperties"] is False
                continue
            assert prop.get("items", {}).get("type") in SCALAR_TYPES, (
                f"{tool_name}: {name} is not a scalar array"
            )


def test_platform_mcp_input_schemas_are_flat_and_agent_friendly() -> None:
    for server in PLATFORM_SERVERS:
        for tool in platform_mcp_tool_manifest(server):
            _assert_agent_friendly_schema(
                tool_name=f"{server}/{tool.name}",
                schema=tool.inputSchema,
            )


def test_diagram_cli_recommends_the_flat_file_preview_contract() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "api/src/vibecanvas_api/agents/prompts/diagram.py").read_text(encoding="utf-8")
    assert 'render_preview(type="file", source=' in source
    assert "flowork-cli diagram" in source
    assert "save_drawio_file" not in source


def test_render_preview_schema_has_no_union_or_nested_view() -> None:
    tool = next(
        tool
        for tool in platform_mcp_tool_manifest("interactive")
        if tool.name == "render_preview"
    )
    serialized = json.dumps(tool.inputSchema)

    assert set(tool.inputSchema["properties"]) == {
        "type",
        "source",
        "title",
        "file_type",
        "description",
        "version",
    }
    assert tool.inputSchema["required"] == ["type"]
    assert tool.inputSchema["properties"]["type"]["enum"] == ["file", "url", "workflow"]
    assert all(token not in serialized for token in ("oneOf", "anyOf", "discriminator"))
    assert "view" not in tool.inputSchema["properties"]
    assert [item.name for item in platform_mcp_tool_manifest("interactive")] == ["render_preview", "render_choices"]
