"""Uniform backend-computed effective access projection."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, Field, model_validator

from vibecanvas_api.authorization.types import Action, Decision
from vibecanvas_api.config import config


class ResourceAccessOut(BaseModel):
    capabilities: list[Action] = Field(default_factory=list)
    effective_role: str | None = None
    source: str = "computed"


class DirectBindingIn(BaseModel):
    relation: Literal["viewer", "editor", "operator", "manager"]
    subject_type: Literal[
        "user",
        "service_account",
        "group",
        "organization",
    ]
    subject_id: str = Field(min_length=1, max_length=512)
    subject_relation: Literal["direct_member", "member"] | None = None

    @model_validator(mode="after")
    def validate_subject_shape(self) -> "DirectBindingIn":
        if self.subject_type == "group" and self.subject_relation is None:
            raise ValueError("group binding requires a membership relation")
        if self.subject_type == "organization":
            if self.subject_relation != "member":
                raise ValueError(
                    "organization-wide binding requires member relation"
                )
        if self.subject_type in {"user", "service_account"}:
            if self.subject_relation is not None:
                raise ValueError("direct principal binding cannot have a relation")
        if self.subject_type == "service_account" and self.relation != "operator":
            raise ValueError("service account binding supports operator only")
        return self


class DirectBindingOut(DirectBindingIn):
    source: Literal["direct"] = "direct"
    display_name: str = ""
    detail: str = ""


class DirectBindingListOut(BaseModel):
    items: list[DirectBindingOut] = Field(default_factory=list)
    continuation_token: str = ""


class ShareTargetLookupIn(BaseModel):
    target_type: Literal["user", "group", "organization"]
    identifier: str = Field(default="", max_length=320)


class ResolvedShareTargetOut(BaseModel):
    target_type: Literal["user", "group", "organization"]
    display_name: str
    detail: str = ""
    resolution_token: str
    allowed_relations: list[
        Literal["viewer", "editor", "operator", "manager"]
    ]


class ShareTargetLookupOut(BaseModel):
    target: ResolvedShareTargetOut | None = None


class DirectBindingGrantIn(BaseModel):
    relation: Literal["viewer", "editor", "operator", "manager"]
    resolution_token: str = Field(min_length=32, max_length=4096)


OwnershipScope: TypeAlias = Literal["personal", "organization", "platform"]
ResourceOrigin: TypeAlias = Literal[
    "created",
    "uploaded",
    "imported",
    "catalog_install",
    "derived",
    "system",
]


class ResourcePartyOut(BaseModel):
    type: Literal["user", "organization", "platform"]
    display_name: str


class ResourceProvenanceOut(BaseModel):
    ownership_scope: OwnershipScope
    origin_type: ResourceOrigin
    owner: ResourcePartyOut
    created_by: ResourcePartyOut | None = None


class SharedResourceOut(BaseModel):
    """Recipient-safe card projection for one explicitly shared root.

    The owning tenant is deliberately absent. The backend uses it only to
    locate and re-authorize the resource; exposing it would leak an internal
    tenancy identifier without helping the recipient navigate the product.
    """

    resource_type: Literal[
        "workflow",
        "task",
        "deployment",
        "knowledge_base",
        "skill_installation",
    ]
    resource_id: str
    name: str
    description: str = ""
    updated_at: datetime
    access: ResourceAccessOut
    provenance: ResourceProvenanceOut


class SharedResourceListOut(BaseModel):
    items: list[SharedResourceOut] = Field(default_factory=list)
    next_offset: int | None = None


def access_from_decision(
    decision: Decision,
    *,
    source: str = "computed",
) -> ResourceAccessOut:
    capabilities = decision.capabilities
    if not config.resource_sharing_enabled:
        capabilities = frozenset(
            action
            for action in capabilities
            if action != Action.MANAGE_ACCESS
        )
    return ResourceAccessOut(
        capabilities=sorted(capabilities, key=str),
        effective_role=decision.effective_role,
        source=source,
    )


def decision_allows_content(decision: Decision) -> bool:
    """Return whether a response may include user-authored content.

    Organization administrators and auditors deliberately receive
    ``view_metadata`` without ``view``. Keeping this check next to the shared
    access projection prevents an allowed inventory decision from being
    mistaken for content authorization by individual list serializers.
    """
    return Action.VIEW in decision.capabilities
