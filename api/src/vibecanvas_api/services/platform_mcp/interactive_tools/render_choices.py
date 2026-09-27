"""Choice contract shared by the host, decision endpoint and MCP catalog."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibecanvas_api.agents.tool_runtime import ToolRuntime, tool
from vibecanvas_api.agents.tools.decorator import ToolError, tool_error_boundary


class ChoiceOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ""


class ChoicesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1)
    options: list[ChoiceOption] = Field(min_length=1)
    description: str = ""
    multiple: bool = False

    @model_validator(mode="after")
    def unique_options(self):
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("Option IDs must be unique.")
        return self

    def selected(self, value: Any) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("selected_ids must be an array of option IDs.")
        if not value or (not self.multiple and len(value) != 1):
            raise ValueError("Select one option." if not self.multiple else "Select at least one option.")
        if len(set(value)) != len(value) or not set(value) <= {option.id for option in self.options}:
            raise ValueError("Selection contains duplicate or unknown option IDs.")
        return value


class ChoicesRequest(BaseModel):
    """Public request; registered download options are resolved by the host."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1)
    options: list[ChoiceOption] = Field(default_factory=list)
    choice_set_id: str = ""
    description: str = ""
    multiple: bool = False

    @model_validator(mode="after")
    def one_source(self):
        if bool(self.options) == bool(self.choice_set_id):
            raise ValueError("Provide either nonempty options or a registered choice_set_id, not both.")
        if self.options:
            ChoicesInput.model_validate(self.model_dump(exclude={"choice_set_id"}))
        return self


@tool(response_format="content_and_artifact")
@tool_error_boundary(tool="render_choices")
async def render_choices(
    title: str,
    options: list[ChoiceOption] = [],
    description: str = "",
    multiple: bool = False,
    choice_set_id: str = "",
    *,
    runtime: ToolRuntime,
) -> tuple[str, dict[str, Any]]:
    """Ask the user to choose, waiting inside this tool call until confirmation or cancellation.

    options contains {id, label, description?}; IDs must be unique. Nothing is
    preselected. multiple=false requires exactly one choice; true allows one or
    more. Returns {status: selected|cancelled|expired, selected_ids, message}.
    Only act on selected_ids when status is selected. Cancellation is not consent.
    Use render_preview for display-only content. Do not call this tool again to
    poll. If the tool runner yields a waiting handle, wait on that same handle;
    do not finish the Agent turn before the original call returns. A selection
    does not approve file transfer. For browser download candidates, pass the
    CLI-returned choice_set_id instead of options, with multiple=false. The host
    supplies authentic filenames and metadata; do not recreate those options.
    """
    # Only the live sandbox Hub can own a waiting call. Never create an orphaned
    # card for direct HTTP invocations that cannot heartbeat/cancel it.
    raise ToolError("interactive_transport_required", "render_choices requires the live Agent MCP Hub transport.")
