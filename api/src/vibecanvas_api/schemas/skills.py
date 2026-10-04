"""Request / response schemas for the ``/api/v1/skills`` routes."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from vibecanvas_api.schemas.access import (
    ResourceAccessOut,
    ResourceProvenanceOut,
)


class SkillOut(BaseModel):
    """Installed Skill response row."""

    id: str
    name: str
    description: str
    allowed_tools: list[str]
    version: int
    installed: bool = False
    created_by_me: bool = False
    source: Optional[str] = None
    source_id: Optional[str] = None
    source_url: Optional[str] = None
    source_revision: Optional[str] = None
    revision_hash: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    access: ResourceAccessOut
    provenance: ResourceProvenanceOut


class SkillDetailOut(SkillOut):
    """Single-skill GET: adds the rendered Markdown body + bundle file paths."""

    body: str
    skill_md: str
    files: list[str]
    has_draft: bool = False
    draft_updated_at: Optional[str] = None


class SkillDraftOut(BaseModel):
    skill_id: str
    base_revision_hash: str
    draft_hash: Optional[str] = None
    skill_md: str
    body: str
    files: list[str]
    has_changes: bool
    updated_at: Optional[str] = None
    access: ResourceAccessOut
    provenance: ResourceProvenanceOut


class SkillDraftSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_md: str = Field(min_length=1)
    expected_hash: str = Field(min_length=1)


class SkillVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(gt=0)
    expected_hash: str = Field(min_length=1)


class SkillRevisionOut(BaseModel):
    revision_id: str
    revision_hash: str
    version: int
    is_latest: bool
    files: list[str]
    size_bytes: int
    created_at: Optional[str] = None
    access: ResourceAccessOut
    provenance: ResourceProvenanceOut


class SkillRevisionDetailOut(SkillRevisionOut):
    name: str
    description: str
    allowed_tools: list[str]
    skill_md: str
    body: str


class SkillCatalogInstall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["openai", "anthropic"]
    source_id: str
