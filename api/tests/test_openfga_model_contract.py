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
        if OPENFGA_OBJECT_TYPES[resource_type] != "template":
            assert "operator" in relations

    for object_type in {
        "chat",
        "template",
        "storage_root",
        "skill_installation",
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
        ResourceType.SKILL_INSTALLATION,
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
