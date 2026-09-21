"""Cross-Runtime schema for Platform MCP ``render_interactive``.

The tool accepts ``Any`` at the Runtime invocation boundary so malformed
model output reaches the tool's normal error envelope instead of becoming an
opaque framework validation failure. ``WithJsonSchema`` still exposes this
strict discriminated union to the model; ``validate_*`` performs the exact same
validation inside the tool before any durable state is written.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    WithJsonSchema,
    field_validator,
)

from vibecanvas_api.agents.tools.decorator import ToolError


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _validate_local_path(value: str) -> str:
    if not value.startswith("/") or ".." in value.split("/"):
        raise ValueError("path must be an absolute local path without '..' segments")
    return value


class HtmlPreviewView(_StrictModel):
    type: Literal["html_preview"]
    html: str = Field(
        min_length=1,
        description=(
            "A complete HTML fragment or document. Self-contained inline JavaScript/CSS plus "
            "data:, blob:, and referenced local VFS files may be used. External scripts and "
            "network subresources are blocked; save a required remote asset into VFS first. "
            "Local files use the same absolute paths available to the Agent."
        ),
    )


class FilePreviewView(_StrictModel):
    type: Literal["file_preview"]
    path: str = Field(
        min_length=1,
        description=(
            "Absolute local file path returned by a file tool. The Preview service detects "
            "the file type and selects the appropriate read-only renderer."
        ),
    )
    file_type: str = Field(
        default="auto",
        min_length=1,
        max_length=64,
        description=(
            "Optional file-type hint passed unchanged to Preview. Use 'auto' unless the file "
            "has no reliable extension or MIME metadata."
        ),
    )
    description: str = ""

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_local_path(value)


class UrlPreviewView(_StrictModel):
    type: Literal["url_preview"]
    url: str = Field(
        min_length=1,
        max_length=8192,
        description=(
            "Absolute HTTP(S) page URL opened in an isolated interactive WebView. "
            "No domain allowlist is applied; the destination may still refuse browser embedding."
        ),
    )
    description: str = ""

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute HTTP(S) URL")
        return value


InteractiveView: TypeAlias = Annotated[
    HtmlPreviewView | FilePreviewView | UrlPreviewView,
    Field(discriminator="type"),
]


_VIEW_ADAPTER = TypeAdapter(InteractiveView)


def interactive_view_json_schema() -> dict[str, Any]:
    """Return the canonical renderer contract used for code generation."""
    schema = deepcopy(_VIEW_ADAPTER.json_schema())
    discriminator = schema.get("discriminator")
    if isinstance(discriminator, dict):
        # Ajv implements the OpenAPI discriminator keyword but deliberately
        # rejects its optional ``mapping`` member. Each branch already has a
        # unique ``type.const``, so propertyName + oneOf is equivalent here.
        discriminator.pop("mapping", None)
    return schema


def _inline_view_json_schema() -> dict[str, Any]:
    """Inline local refs for the enclosing tool-call schema.

    ``WithJsonSchema`` is embedded below another Pydantic model. Local ``$defs``
    from the supplied fragment are not hoisted by Pydantic, leaving broken refs;
    the frontend generator can keep the canonical ref form while the model sees
    this equivalent self-contained union.
    """
    schema = interactive_view_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value: Any) -> Any:
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.rsplit("/", 1)[-1]
            return expand(deepcopy(definitions[name]))
        return {key: expand(item) for key, item in value.items()}

    inlined = expand(schema)
    discriminator = inlined.get("discriminator")
    if isinstance(discriminator, dict):
        discriminator.pop("mapping", None)
    return inlined

# Precise JSON schemas for the model, permissive runtime types for our own
# agent-readable validation errors inside the decorated tool body.
ViewArgument: TypeAlias = Annotated[Any, WithJsonSchema(_inline_view_json_schema())]


def _validation_tool_error(field: str, exc: ValidationError) -> ToolError:
    errors = []
    messages = []
    for error in exc.errors(include_url=False)[:8]:
        location = ".".join(str(part) for part in error.get("loc", ())) or field
        message = str(error.get("msg") or "invalid value")
        errors.append({"field": location, "message": message, "type": error.get("type")})
        messages.append(f"{location}: {message}")
    detail = "; ".join(messages) or f"{field} does not match the required schema"
    return ToolError(
        "invalid_interactive_input",
        f"Invalid render_interactive {field}: {detail}. Fix these fields and call the tool again.",
        info={"field": field, "errors": errors},
    )


def validate_view(value: Any) -> InteractiveView:
    try:
        return _VIEW_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise _validation_tool_error("view", exc) from exc
