"""Static drift guards between the application registry and model JSON."""

from __future__ import annotations

import json
from pathlib import Path

from vibecanvas_api.authorization.openfga_model import (
    ACTION_RELATIONS,
    OPENFGA_OBJECT_TYPES,
    SHAREABLE_RESOURCE_TYPES,
)
from vibecanvas_api.authorization.types import ResourceType


MODEL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "vibecanvas_api"
    / "authorization"
    / "model"
)


def _model_types() -> dict[str, dict]:
    model = json.loads((MODEL_DIR / "model.json").read_text(encoding="utf-8"))
    assert model["schema_version"] == "1.2"
    return {
        item["type"]: item
        for item in model["type_definitions"]
    }


def test_every_application_action_maps_to_a_model_permission():
    types = _model_types()
    assert set(OPENFGA_OBJECT_TYPES.values()) == set(types) - {"user"}
    assert set(ACTION_RELATIONS) == set(OPENFGA_OBJECT_TYPES)
    for resource_type, action_relations in ACTION_RELATIONS.items():
        model_type = types[OPENFGA_OBJECT_TYPES[resource_type]]
        for relation in action_relations.values():
            assert relation.startswith("can_")
            assert relation in model_type["relations"]


def test_shareable_and_private_types_do_not_drift():
    types = _model_types()
    for resource_type in SHAREABLE_RESOURCE_TYPES:
        relations = set(
            types[OPENFGA_OBJECT_TYPES[resource_type]]["relations"]
        )
        assert {"viewer", "editor", "manager"} <= relations
        if resource_type not in {ResourceType.SKILL_INSTALLATION, ResourceType.KNOWLEDGE_BASE}:
            assert "operator" in relations

    for object_type in {
        "chat",
        "template",
        "storage_root",
        "mcp_installation",
        "llm_credential",
    }:
        relations = set(types[object_type]["relations"])
        # Credentials have an internal ``manager`` relation for
        # use-without-reveal administration. It is deliberately not exposed
        # by SHARE_RELATION_SUBJECTS and therefore is not a generic share role.
        assert not relations & {"viewer", "editor", "operator"}
    assert {
        ResourceType.CHAT,
        ResourceType.TEMPLATE,
        ResourceType.STORAGE_ROOT,
        ResourceType.MCP_INSTALLATION,
        ResourceType.LLM_CREDENTIAL,
    }.isdisjoint(SHAREABLE_RESOURCE_TYPES)


def test_model_has_no_public_wildcard_or_pii_fixture():
    for path in MODEL_DIR.glob("*"):
        if path.suffix not in {".fga", ".mod", ".yaml", ".json"}:
            continue
        body = path.read_text(encoding="utf-8").lower()
        assert "user:*" not in body
        assert "public:*" not in body
        assert "@" not in body


def test_modular_manifest_references_exact_checked_in_modules():
    manifest = (MODEL_DIR / "fga.mod").read_text(encoding="utf-8")
    assert "schema: \"1.2\"" in manifest
    assert {
        line.strip()[2:]
        for line in manifest.splitlines()
        if line.strip().startswith("- ")
    } == {
        "core.fga",
        "private_resources.fga",
        "collaborative_resources.fga",
    }


def _references_relation(value: object, relation: str) -> bool:
    if isinstance(value, dict):
        computed = value.get("computedUserset")
        if isinstance(computed, dict) and computed.get("relation") == relation:
            return True
        return any(_references_relation(child, relation) for child in value.values())
    if isinstance(value, list):
        return any(_references_relation(child, relation) for child in value)
    return False


def test_organization_auditor_is_metadata_only_for_every_resource_type():
    """Audit membership must never imply content/run/secret capabilities."""
    types = _model_types()
    for resource_type, action_relations in ACTION_RELATIONS.items():
        if resource_type is ResourceType.ORGANIZATION:
            continue
        model_relations = types[OPENFGA_OBJECT_TYPES[resource_type]]["relations"]
        assert _references_relation(
            model_relations["can_view_metadata"],
            "can_view_audit",
        ), resource_type
        for action, relation in action_relations.items():
            if relation == "can_view_metadata":
                continue
            assert not _references_relation(
                model_relations[relation],
                "can_view_audit",
            ), (resource_type, action)


def test_workflow_resource_delegates_get_use_without_management_or_secret_access():
    types = _model_types()
    for kind in ("skill_installation", "mcp_installation"):
        model = types[kind]
        assert model["metadata"]["relations"]["consumer"]["directly_related_user_types"] == [{"type": "service_account"}]
        assert _references_relation(model["relations"]["can_use"], "consumer")
        for name, relation in model["relations"].items():
            if name.startswith("can_") and name != "can_use":
                assert not _references_relation(relation, "consumer")


def test_shared_skill_roles_do_not_grant_execution_or_credential_access():
    from vibecanvas_api.authorization.openfga_model import ROLE_CAPABILITIES, SHARE_RELATION_SUBJECTS
    from vibecanvas_api.authorization.types import Action
    roles = ROLE_CAPABILITIES[ResourceType.SKILL_INSTALLATION]
    assert set(SHARE_RELATION_SUBJECTS[ResourceType.SKILL_INSTALLATION]) == {"viewer", "editor", "manager"}
    assert roles["viewer"] == {Action.VIEW_METADATA, Action.VIEW, Action.USE}
    assert roles["editor"] == roles["viewer"] | {Action.UPDATE, Action.PUBLISH}
    assert Action.MANAGE_ACCESS in roles["manager"]
    assert all(Action.MANAGE_SECRET not in actions for actions in roles.values())


def test_share_registry_subjects_match_model():
    from vibecanvas_api.authorization.openfga_model import SHARE_RELATION_SUBJECTS, SHARE_ROLES
    types = _model_types()
    assert set(SHARE_ROLES) == SHAREABLE_RESOURCE_TYPES
    for resource_type, roles in SHARE_RELATION_SUBJECTS.items():
        model = types[OPENFGA_OBJECT_TYPES[resource_type]]
        assert set(roles) == set(SHARE_ROLES[resource_type])
        for role, subjects in roles.items():
            actual = {
                (item["type"], item.get("relation"))
                for item in model["metadata"]["relations"][role]["directly_related_user_types"]
            }
            assert actual == {(kind.value, relation) for kind, relation in subjects}


def test_skill_model_read_and_publish_capabilities_match_registry():
    from vibecanvas_api.authorization.openfga_model import ROLE_CAPABILITIES
    model = _model_types()["skill_installation"]
    for role, expected in ROLE_CAPABILITIES[ResourceType.SKILL_INSTALLATION].items():
        for action, relation in ACTION_RELATIONS[ResourceType.SKILL_INSTALLATION].items():
            assert _references_relation(model["relations"][relation], role) == (action in expected), (role, action)


def test_company_membership_includes_administrators_but_not_guests():
    member = _model_types()["organization"]["relations"]["member"]
    assert _references_relation(member, "owner")
    assert _references_relation(member, "admin")
    assert not _references_relation(member, "guest")
    assert not _references_relation(member, "auditor")
    assert {"this": {}} in member["union"]["child"]


def test_company_can_receive_each_supported_share_role():
    from vibecanvas_api.authorization.openfga_model import SHARE_RELATION_SUBJECTS
    from vibecanvas_api.authorization.types import RelationshipSubjectType
    for roles in SHARE_RELATION_SUBJECTS.values():
        for subjects in roles.values():
            assert (RelationshipSubjectType.ORGANIZATION, "member") in subjects


def test_knowledge_readers_can_use_content_without_a_separate_operator_role():
    from vibecanvas_api.authorization.openfga_model import ROLE_CAPABILITIES, SHARE_ROLES
    from vibecanvas_api.authorization.types import Action
    kind = ResourceType.KNOWLEDGE_BASE
    assert SHARE_ROLES[kind] == ("viewer", "editor", "manager")
    model = _model_types()["knowledge_base"]
    assert "operator" not in model["relations"]
    roles = ROLE_CAPABILITIES[kind]
    assert roles["viewer"] == {Action.VIEW_METADATA, Action.VIEW, Action.USE}
    assert roles["editor"] == roles["viewer"] | {Action.UPDATE}
    for role, expected in roles.items():
        for action, relation in ACTION_RELATIONS[kind].items():
            assert _references_relation(model["relations"][relation], role) == (action in expected), (role, action)


def test_instance_history_is_visible_to_all_content_roles():
    types = _model_types()
    for kind in ("task", "deployment"):
        children = types[kind]["relations"]["can_inspect_runs"]["union"]["child"]
        assert {child["computedUserset"]["relation"] for child in children} == {
            "viewer", "editor", "operator", "manager",
        }
    assert types["workflow"]["relations"]["can_inspect_runs"] != types["deployment"]["relations"]["can_inspect_runs"]
