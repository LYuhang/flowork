"""Typed context attachments. Parsing is not resource authorization.

The host must resolve references under the destination Chat's permissions
before accepting a draft operation or dispatching a turn.
"""
from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from vibecanvas_api.schemas.preview import FileRefV1

Identifier = Annotated[str, Field(min_length=1, max_length=512)]


class ContextModel(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)


class MessageSource(ContextModel):
    kind: Literal['message']
    chat_id: Identifier
    message_id: Identifier


class FileResource(ContextModel):
    kind: Literal['file']
    file_ref: FileRefV1
    revision: Annotated[str, Field(min_length=1, max_length=256)] | None = None


class WorkflowResource(ContextModel):
    kind: Literal['workflow']
    workflow_id: Identifier
    version: str = Field(pattern=r'^v[1-9]\d*\.sv\d+$', max_length=64)


class ArtifactResource(ContextModel):
    kind: Literal['artifact']
    chat_id: Identifier
    artifact_id: Identifier


class JobResource(ContextModel):
    kind: Literal['job']
    chat_id: Identifier
    job_id: Identifier
    execution_id: Identifier | None = None


class WebResource(ContextModel):
    kind: Literal['web']
    url: str = Field(min_length=1, max_length=4096, pattern=r'^https?://')

    @field_validator('url')
    @classmethod
    def validate_public_reference(cls, value: str) -> str:
        # A reference is not a fetch permission. Keep credentials out of the
        # durable transcript; the browser/runtime enforces its own fetch policy.
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError('web references require a hostname and no embedded credentials')
        if any(char.isspace() or ord(char) < 32 for char in value) or '\\' in value:
            raise ValueError('web reference contains invalid URL characters')
        # Accessing .port also validates malformed or out-of-range ports.
        _ = parsed.port
        return value


ContextResource = Annotated[FileResource | WorkflowResource | ArtifactResource | JobResource | WebResource, Field(discriminator='kind')]
QuoteSource = Annotated[MessageSource | FileResource | ArtifactResource | JobResource | WebResource, Field(discriminator='kind')]


class TextSelection(ContextModel):
    kind: Literal['text']
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    unsaved: bool = False

    @model_validator(mode='after')
    def check_range(self):
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError('text range requires both start and end lines')
        if self.start_line is not None and self.end_line < self.start_line:
            raise ValueError('text range is reversed')
        return self


class WebSelection(ContextModel):
    """Browser-captured location hints; never authorization or executable code."""
    kind: Literal['web_selection']
    tab_id: str | None = Field(default=None, pattern=r'^tab_[A-Za-z0-9_-]+$', max_length=256)
    window_id: str | None = Field(default=None, pattern=r'^win_[0-9]+$', max_length=64)
    title: str = Field(default='', max_length=512)
    frame_url: str | None = Field(default=None, max_length=4096)
    css_selector: str | None = Field(default=None, max_length=2048)
    prefix: str = Field(default='', max_length=512)
    suffix: str = Field(default='', max_length=512)

    @model_validator(mode='after')
    def check_browser_target(self):
        if (self.tab_id is None) != (self.window_id is None):
            raise ValueError('browser reference requires both tab_id and window_id')
        return self

    @field_validator('frame_url')
    @classmethod
    def validate_frame_url(cls, value: str | None) -> str | None:
        if value is not None:
            return WebResource(kind='web', url=value).url
        return None


class PageSelection(ContextModel):
    kind: Literal['pages']
    pages: list[Annotated[int, Field(ge=1)]] = Field(min_length=1, max_length=100)
    rendition_revision: str | None = Field(default=None, max_length=256)


class CellSelection(ContextModel):
    kind: Literal['cells']
    sheet: str = Field(min_length=1, max_length=256)
    # Original, one-based coordinates, never sorted/filtered viewport indices.
    first_row: int = Field(ge=1)
    last_row: int = Field(ge=1)
    first_column: int = Field(ge=1)
    last_column: int = Field(ge=1)

    @model_validator(mode='after')
    def check_range(self):
        if self.last_row < self.first_row or self.last_column < self.first_column:
            raise ValueError('cell range is reversed')
        return self


class TimeSelection(ContextModel):
    kind: Literal['time']
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def check_range(self):
        if self.end_seconds is not None and self.end_seconds < self.start_seconds:
            raise ValueError('time range is reversed')
        return self


class EdgeSelection(ContextModel):
    source: Identifier
    target: Identifier
    source_handle: Identifier | None = None
    target_handle: Identifier | None = None


class WorkflowSelection(ContextModel):
    kind: Literal['workflow_elements']
    node_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    edges: list[EdgeSelection] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def check_elements(self):
        if not self.node_ids and not self.edges:
            raise ValueError('workflow selection must not be empty')
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError('duplicate workflow nodes')
        return self


ContextSelection = Annotated[TextSelection | PageSelection | CellSelection | TimeSelection | WorkflowSelection, Field(discriminator='kind')]


class QuoteSnapshot(ContextModel):
    text: str = Field(min_length=1, max_length=32768)


class AttachmentBase(ContextModel):
    schema_version: Literal[1] = 1
    id: Identifier
    label: str = Field(min_length=1, max_length=512)


class ResourceSnapshot(ContextModel):
    text: str = Field(max_length=65536)


class FileContextAttachment(AttachmentBase):
    type: Literal['file']
    resource: FileResource
    content_type: str = Field(min_length=1, max_length=256)
    size_bytes: int | None = Field(default=None, ge=0)
    # Derived by the host, including the durable sandbox copy location.
    snapshot: ResourceSnapshot | None = None


class QuoteContextAttachment(AttachmentBase):
    type: Literal['quote']
    source: QuoteSource
    snapshot: QuoteSnapshot
    selector: TextSelection | WebSelection | None = None

    @model_validator(mode='after')
    def check_web_selection(self):
        if isinstance(self.selector, WebSelection) and self.source.kind != 'web':
            raise ValueError('web selection requires a web source')
        return self


class ResourceContextAttachment(AttachmentBase):
    type: Literal['resource']
    resource: ContextResource
    # Host replaces this on dispatch; supplied snapshots never grant access.
    snapshot: ResourceSnapshot | None = None
    selector: ContextSelection | None = None

    @model_validator(mode='after')
    def check_selection(self):
        if self.selector is None:
            return self
        allowed = {
            'workflow': {'workflow_elements'},
            'file': {'text', 'pages', 'cells', 'time'},
            'artifact': {'text'},
            'job': {'text'},
            'web': {'text'},
        }
        if self.selector.kind not in allowed[self.resource.kind]:
            raise ValueError('selection is not supported for this resource')
        return self


ContextAttachment = Annotated[FileContextAttachment | QuoteContextAttachment | ResourceContextAttachment, Field(discriminator='type')]
