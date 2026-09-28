"""Versioned application-internal Preview resource contracts."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _PreviewModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


def _path_with_prefix(value: str, prefix: str) -> str:
    if not value.startswith(prefix) or ".." in value.split("/") or "\x00" in value:
        raise ValueError(f"path must start with {prefix!r} and contain no traversal")
    return value


class ProjectFileRefV1(_PreviewModel):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    scope: Literal["project"]
    project_id: str = Field(alias="projectId", min_length=1)
    path: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        for prefix in ("/data/", "/memory/", "/logs/", "/chats/"):
            if value.startswith(prefix):
                return _path_with_prefix(value, prefix)
        raise ValueError(
            "workspace path must start with '/data/', '/memory/', '/logs/', or '/chats/'"
        )


class MountFileRefV1(_PreviewModel):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    scope: Literal["mount"]
    path: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _path_with_prefix(value, "/mount/")


class RunFileRefV1(_PreviewModel):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    scope: Literal["run"]
    run_id: str = Field(alias="runId", min_length=1)
    path: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _path_with_prefix(value, "/run/")


FileRefV1 = Annotated[
    ProjectFileRefV1 | MountFileRefV1 | RunFileRefV1,
    Field(discriminator="scope"),
]


class PreviewResolveBody(_PreviewModel):
    file_ref: FileRefV1 = Field(alias="fileRef")


class PreviewResourceMount(_PreviewModel):
    path_prefix: str = Field(alias="pathPrefix")
    root_url: str = Field(alias="rootUrl")


class PreviewResourceSession(_PreviewModel):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    resource_mounts: list[PreviewResourceMount] = Field(alias="resourceMounts")
    base_url: str = Field(alias="baseUrl")
    expires_in: int = Field(alias="expiresIn")


class PreviewCapabilities(_PreviewModel):
    preview: bool
    edit: bool
    download: bool


class PreviewContent(_PreviewModel):
    inline_text: str | None = Field(default=None, alias="inlineText")
    url: str | None = None
    truncated: bool = False
    range_supported: bool = Field(default=False, alias="rangeSupported")


class PreviewRendition(_PreviewModel):
    format: Literal["pdf"]
    content_type: Literal["application/pdf"] = Field(alias="contentType")
    url: str
    source_revision: str = Field(alias="sourceRevision")


class PreviewTextMetadata(_PreviewModel):
    encoding: Literal["utf-8"] = "utf-8"
    bom: bool = False
    newline: Literal["LF", "CRLF"] = "LF"
    mixed_newlines: bool = Field(default=False, alias="mixedNewlines")


class PreviewErrorInfo(_PreviewModel):
    code: str = Field(min_length=1, max_length=128)
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)


class PreviewDescriptorV1(_PreviewModel):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    file_ref: FileRefV1 = Field(alias="fileRef")
    name: str
    size_bytes: int = Field(alias="sizeBytes")
    content_type: str = Field(alias="contentType")
    detected_type: str = Field(alias="detectedType")
    revision: str
    renderer: Literal[
        "text", "markdown", "html", "pdf", "docx", "pptx",
        "spreadsheet", "image", "audio", "video", "drawio",
        "unsupported",
    ]
    load_policy: Literal[
        "inline", "stream", "range", "manual", "unsupported",
    ] = Field(alias="loadPolicy")
    capabilities: PreviewCapabilities
    content: PreviewContent | None = None
    rendition: PreviewRendition | None = None
    text: PreviewTextMetadata | None = None
    diagram: dict[str, Any] | None = None
    error: PreviewErrorInfo | None = None


class PreviewFileWriteV1(_PreviewModel):
    file_ref: FileRefV1 = Field(alias="fileRef")
    expected_revision: str = Field(alias="expectedRevision", min_length=1)
    content_type: str = Field(alias="contentType", min_length=1)
    content: str


class PreviewFileWriteOut(_PreviewModel):
    file_ref: FileRefV1 = Field(alias="fileRef")
    revision: str
    size_bytes: int = Field(alias="sizeBytes")
    content_type: str = Field(alias="contentType")
