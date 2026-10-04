"""Authoritative application-to-OpenFGA vocabulary mapping.

Product code speaks :class:`Action` and :class:`ResourceType`.  Only this
module knows the OpenFGA object, computed-permission, and direct-relation
names.  Keeping the mapping executable prevents routes and workers from
inventing relation strings that drift from the checked-in model.
"""

from __future__ import annotations

from collections.abc import Mapping

from .types import (
    Action,
    RelationshipBinding,
    RelationshipSubjectType,
    ResourceType,
)


OPENFGA_OBJECT_TYPES: Mapping[ResourceType, str] = {
    ResourceType.ORGANIZATION: "organization",
    ResourceType.GROUP: "group",
    ResourceType.CHAT: "chat",
    ResourceType.WORKFLOW: "workflow",
    ResourceType.TEMPLATE: "template",
    ResourceType.TASK: "task",
    ResourceType.DEPLOYMENT: "deployment",
    ResourceType.STORAGE_ROOT: "storage_root",
    ResourceType.KNOWLEDGE_BASE: "knowledge_base",
    ResourceType.MCP_INSTALLATION: "mcp_installation",
    ResourceType.SKILL_INSTALLATION: "skill_installation",
    ResourceType.LLM_CREDENTIAL: "llm_credential",
    ResourceType.SERVICE_ACCOUNT: "service_account",
}


ACTION_RELATIONS: Mapping[ResourceType, Mapping[Action, str]] = {
    ResourceType.ORGANIZATION: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.CREATE: "can_create_resource",
        Action.DELETE: "can_delete",
        Action.TRANSFER: "can_transfer",
        Action.MANAGE_MEMBERS: "can_manage_members",
        Action.MANAGE_POLICY: "can_manage_policy",
        Action.VIEW_AUDIT: "can_view_audit",
    },
    ResourceType.GROUP: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.MANAGE_MEMBERS: "can_manage_members",
    },
    ResourceType.CHAT: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.EXPORT: "can_export",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.USE: "can_use",
        Action.EXECUTE: "can_execute",
        Action.CANCEL: "can_cancel",
        Action.RESUME: "can_resume",
        Action.INSPECT_RUNS: "can_inspect_runs",
        Action.MOUNT: "can_mount",
    },
    ResourceType.WORKFLOW: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.EXPORT: "can_export",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.MANAGE_ACCESS: "can_manage_access",
        Action.USE: "can_use",
        Action.EXECUTE: "can_execute",
        Action.CANCEL: "can_cancel",
        Action.INSPECT_RUNS: "can_inspect_runs",
        Action.DEPLOY: "can_deploy",
        Action.MOUNT: "can_mount",
    },
    ResourceType.TEMPLATE: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.USE: "can_use",
        Action.PUBLISH: "can_publish",
    },
    ResourceType.TASK: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.EXPORT: "can_export",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.MANAGE_ACCESS: "can_manage_access",
        Action.EXECUTE: "can_execute",
        Action.CANCEL: "can_cancel",
        Action.RESUME: "can_resume",
        Action.INSPECT_RUNS: "can_inspect_runs",
    },
    ResourceType.DEPLOYMENT: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.MANAGE_ACCESS: "can_manage_access",
        Action.EXECUTE: "can_execute",
        Action.CANCEL: "can_cancel",
        Action.INSPECT_RUNS: "can_inspect_runs",
        Action.MANAGE_SECRET: "can_manage_secret",
    },
    ResourceType.STORAGE_ROOT: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.MOUNT: "can_mount",
    },
    ResourceType.KNOWLEDGE_BASE: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.MANAGE_ACCESS: "can_manage_access",
        Action.USE: "can_use",
    },
    ResourceType.SKILL_INSTALLATION: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.MANAGE_ACCESS: "can_manage_access",
        Action.USE: "can_use",
        Action.PUBLISH: "can_publish",
    },
    ResourceType.MCP_INSTALLATION: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.VIEW: "can_view",
        Action.UPDATE: "can_update",
        Action.DELETE: "can_delete",
        Action.USE: "can_use",
        Action.MANAGE_SECRET: "can_manage_secret",
    },
    ResourceType.LLM_CREDENTIAL: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.USE: "can_use",
        Action.MANAGE_SECRET: "can_manage_secret",
        Action.DELETE: "can_delete",
    },
    ResourceType.SERVICE_ACCOUNT: {
        Action.VIEW_METADATA: "can_view_metadata",
        Action.UPDATE: "can_update",
        Action.MANAGE_SECRET: "can_manage_secret",
        Action.DELETE: "can_delete",
    },
}


SHAREABLE_RESOURCE_TYPES = frozenset({
    ResourceType.WORKFLOW,
    ResourceType.TASK,
    ResourceType.DEPLOYMENT,
    ResourceType.KNOWLEDGE_BASE,
    ResourceType.SKILL_INSTALLATION,
})


_USER = RelationshipSubjectType.USER
_SERVICE_ACCOUNT = RelationshipSubjectType.SERVICE_ACCOUNT
_GROUP = RelationshipSubjectType.GROUP
_ORGANIZATION = RelationshipSubjectType.ORGANIZATION

# One role/subject registry is shared by authorization checks and share UIs.
# Package content has no separate execute role: readers can use downloaded files.
SHARE_ROLES: Mapping[ResourceType, tuple[str, ...]] = {
    resource_type: (
        ("viewer", "editor", "manager")
        if resource_type in {ResourceType.SKILL_INSTALLATION, ResourceType.KNOWLEDGE_BASE}
        else ("viewer", "editor", "operator", "manager")
    )
    for resource_type in SHAREABLE_RESOURCE_TYPES
}

_ROLE_SUBJECTS = {
    "viewer": frozenset({
        (_USER, None), (_GROUP, "direct_member"), (_GROUP, "member"),
        (_ORGANIZATION, "member"),
    }),
    "editor": frozenset({
        (_USER, None), (_GROUP, "direct_member"), (_GROUP, "member"),
        (_ORGANIZATION, "member"),
    }),
    "operator": frozenset({
        (_USER, None), (_SERVICE_ACCOUNT, None),
        (_GROUP, "direct_member"), (_GROUP, "member"),
        (_ORGANIZATION, "member"),
    }),
    "manager": frozenset({
        (_USER, None), (_GROUP, "direct_member"), (_GROUP, "member"),
        (_ORGANIZATION, "member"),
    }),
}

SHARE_RELATION_SUBJECTS: Mapping[
    ResourceType,
    Mapping[str, frozenset[tuple[RelationshipSubjectType, str | None]]],
] = {
    resource_type: {role: _ROLE_SUBJECTS[role] for role in roles}
    for resource_type, roles in SHARE_ROLES.items()
}

ROLE_CAPABILITIES: Mapping[
    ResourceType,
    Mapping[str, frozenset[Action]],
] = {
    ResourceType.CHAT: {
        "creator": frozenset(ACTION_RELATIONS[ResourceType.CHAT]),
    },
    ResourceType.MCP_INSTALLATION: {
        "installer": frozenset(
            ACTION_RELATIONS[ResourceType.MCP_INSTALLATION]
        ),
    },
    ResourceType.LLM_CREDENTIAL: {
        "owner": frozenset(ACTION_RELATIONS[ResourceType.LLM_CREDENTIAL]),
        "manager": frozenset({
            Action.VIEW_METADATA,
            Action.MANAGE_SECRET,
            Action.DELETE,
        }),
        "consumer": frozenset({Action.USE}),
    },
    ResourceType.WORKFLOW: {
        "viewer": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.EXPORT,
            Action.USE,
            Action.MOUNT,
        }),
        "editor": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.EXPORT,
            Action.UPDATE,
            Action.USE,
            Action.MOUNT,
        }),
        "operator": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.EXPORT,
            Action.USE,
            Action.EXECUTE,
            Action.CANCEL,
            Action.INSPECT_RUNS,
            Action.MOUNT,
        }),
        "manager": frozenset(ACTION_RELATIONS[ResourceType.WORKFLOW]),
    },
    ResourceType.TEMPLATE: {
        "manager": frozenset(ACTION_RELATIONS[ResourceType.TEMPLATE]),
    },
    ResourceType.TASK: {
        "viewer": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.EXPORT,
            Action.INSPECT_RUNS,
        }),
        "editor": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.EXPORT,
            Action.UPDATE,
            Action.INSPECT_RUNS,
        }),
        "operator": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.EXPORT,
            Action.EXECUTE,
            Action.CANCEL,
            Action.RESUME,
            Action.INSPECT_RUNS,
        }),
        "manager": frozenset(ACTION_RELATIONS[ResourceType.TASK]),
    },
    ResourceType.DEPLOYMENT: {
        "viewer": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.INSPECT_RUNS,
        }),
        "editor": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.INSPECT_RUNS,
            Action.UPDATE,
        }),
        "operator": frozenset({
            Action.VIEW_METADATA,
            Action.VIEW,
            Action.EXECUTE,
            Action.CANCEL,
            Action.INSPECT_RUNS,
        }),
        "manager": frozenset(ACTION_RELATIONS[ResourceType.DEPLOYMENT]),
    },
    ResourceType.STORAGE_ROOT: {
        "manager": frozenset(ACTION_RELATIONS[ResourceType.STORAGE_ROOT]),
    },
    ResourceType.KNOWLEDGE_BASE: {
        "viewer": frozenset({Action.VIEW_METADATA, Action.VIEW, Action.USE}),
        "editor": frozenset({Action.VIEW_METADATA, Action.VIEW, Action.USE, Action.UPDATE}),
        "manager": frozenset(ACTION_RELATIONS[ResourceType.KNOWLEDGE_BASE]),
    },
    ResourceType.SKILL_INSTALLATION: {
        "viewer": frozenset({Action.VIEW_METADATA, Action.VIEW, Action.USE}),
        "editor": frozenset({Action.VIEW_METADATA, Action.VIEW, Action.USE, Action.UPDATE, Action.PUBLISH}),
        "manager": frozenset(
            ACTION_RELATIONS[ResourceType.SKILL_INSTALLATION]
        ),
    },
}


def openfga_object(resource_type: ResourceType, resource_id: str) -> str:
    object_type = OPENFGA_OBJECT_TYPES.get(resource_type)
    if object_type is None:
        raise ValueError(f"unsupported OpenFGA resource type: {resource_type}")
    _validate_entity_id(resource_id)
    return f"{object_type}:{resource_id}"


def openfga_principal(principal_type: str, principal_id: str) -> str:
    if principal_type not in {"user", "service_account"}:
        raise ValueError(f"unsupported OpenFGA principal type: {principal_type}")
    _validate_entity_id(principal_id)
    return f"{principal_type}:{principal_id}"


def action_relation(resource_type: ResourceType, action: Action) -> str:
    relation = ACTION_RELATIONS.get(resource_type, {}).get(action)
    if relation is None:
        raise ValueError(
            f"{resource_type.value} does not support action {action.value}"
        )
    return relation


def validate_share_binding(binding: RelationshipBinding) -> None:
    relations = SHARE_RELATION_SUBJECTS.get(binding.resource.type)
    if relations is None:
        raise ValueError(
            f"{binding.resource.type.value} does not support direct sharing"
        )
    allowed_subjects = relations.get(binding.relation)
    if allowed_subjects is None:
        raise ValueError(
            f"unsupported share relation {binding.relation!r} for "
            f"{binding.resource.type.value}"
        )
    subject_key = (binding.subject.type, binding.subject.relation)
    if subject_key not in allowed_subjects:
        raise ValueError(
            f"subject {binding.subject.type.value}#{binding.subject.relation or ''} "
            f"cannot receive {binding.relation}"
        )
    _validate_entity_id(binding.subject.id)
    _validate_entity_id(binding.resource.id)


def effective_role(
    resource_type: ResourceType,
    capabilities: frozenset[Action],
) -> str | None:
    for role, expected in ROLE_CAPABILITIES.get(resource_type, {}).items():
        if capabilities == expected:
            return role
    return "custom" if capabilities else None


def _validate_entity_id(value: str) -> None:
    if (
        not value
        or ":" in value
        or "#" in value
        or "\n" in value
        or "\r" in value
        or len(value) > 512
    ):
        raise ValueError("OpenFGA entity id is invalid")
