"""Workflow + version-tree request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .access import ResourceAccessOut, ResourceProvenanceOut


class WorkflowCreate(BaseModel):
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class WorkflowMetaOut(BaseModel):
    wf_id: str
    workflow_name: str
    description: str
    active_v: int
    active_sv: int
    updated_at: float
    created_at: float
    tags: list[str] = Field(default_factory=list)
    access: ResourceAccessOut
    provenance: ResourceProvenanceOut


class WorkflowSnapshotOut(BaseModel):
    workflow: dict
    meta: WorkflowMetaOut


class WorkflowMetaPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class EditsRequest(BaseModel):
    updates: list[list]


class EditsResponse(BaseModel):
    applied_count: int
    total_count: int
    new_meta: WorkflowMetaOut
    first_error: str | None = None
    first_error_index: int | None = None


class CommitRequest(BaseModel):
    workflow: dict
    note: str = ""
    # UX-5: when the client is editing a PINNED historical major version, it
    # passes that major here so the commit lands under it (new sub) instead of
    # the active major. ``None`` (default) preserves the existing contract:
    # commit to the active major and advance its sub. ``new_major-versions``
    # ignores this field (it always allocates a fresh major).
    target_major: int | None = None


class GuardedCommitRequest(CommitRequest):
    expected_version: str = Field(pattern=r"^v[1-9]\d*\.sv\d+$")


class CheckRequest(BaseModel):
    # Optional in-progress DRAFT to validate. When present, Check validates THIS
    # (so the user can Check unsaved edits without saving first); when absent,
    # the route falls back to the committed current version.
    workflow: dict | None = None


class CheckResponse(BaseModel):
    status: str
    error_message: str | None = None


class CheckoutRequest(BaseModel):
    v: int
    sv: int


class PromptHistoryOut(BaseModel):
    node_id: str
    prompts: list[str]
    current: str
