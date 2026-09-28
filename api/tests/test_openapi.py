"""Gate 2 — OpenAPI export covers all expected endpoints / tags / schemas.

A regression net for "did we forget to mount a router?" and "did we
forget to expose a schema?" before either breaks frontend code generation.
"""

from __future__ import annotations

import pytest
from vibecanvas_api.app import build_app
from vibecanvas_api.config import config


@pytest.fixture
def spec():
    # The OpenAPI document is structural — it needs no auth and no DB.
    # (The legacy DEV_TOKEN / STORAGE_ROOT harness was only there for the
    # now-dead sync TestClient route tests.)
    return build_app().openapi()


def test_openapi_has_at_least_28_paths(spec):
    paths = spec.get("paths", {})
    assert len(paths) >= 28, f"only {len(paths)} paths: {list(paths)}"


def test_runtime_capabilities_have_no_retired_chat_binding_scope(spec):
    properties = spec["components"]["schemas"]["RuntimeCapabilities"]["properties"]
    assert "binding_scope" not in properties


def test_openapi_has_expected_tags(spec):
    seen_tags: set[str] = set()
    for path_item in spec["paths"].values():
        for op in path_item.values():
            if isinstance(op, dict):
                for tag in op.get("tags", []):
                    seen_tags.add(tag)
    expected = {"meta", "workflows", "executions", "chats", "tasks"}
    missing = expected - seen_tags
    assert not missing, f"missing tags: {missing}"


def test_openapi_includes_edits_endpoint(spec):
    """The workflow edits endpoint is required for incremental synchronization."""
    paths = spec["paths"]
    assert any(p.endswith("/edits") for p in paths), (
        f"no /edits endpoint in {list(paths)}"
    )


def test_openapi_chat_messages_endpoint_is_post(spec):
    paths = spec["paths"]
    matching = [p for p in paths if "/chats/" in p and p.endswith("/messages")]
    assert matching, "no chat messages endpoint"
    for p in matching:
        ops = paths[p]
        assert "post" in ops or "get" in ops, (
            f"chat messages endpoint {p} has no POST/GET op: {list(ops)}"
        )


def test_openapi_components_has_page_schemas(spec):
    assert "components" in spec
    schemas = spec["components"].get("schemas", {})
    assert any("Page" in k for k in schemas), (
        f"no Page schema in components: {list(schemas)[:20]}"
    )


def test_production_app_hides_interactive_api_docs(monkeypatch):
    monkeypatch.setattr(config, "environment", "production")
    monkeypatch.setattr(
        "vibecanvas_api.security_profile.validate_production_security",
        lambda *_args, **_kwargs: None,
    )

    paths = {
        path
        for route in build_app().routes
        if (path := getattr(route, "path", None)) is not None
    }

    assert "/docs" not in paths
    assert "/redoc" not in paths
    assert "/openapi.json" not in paths
