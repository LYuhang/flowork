from __future__ import annotations

import json
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact import (
    _persist_interactive_state,
)
from vibecanvas_api.services.platform_mcp.interactive_tools.render_preview import render_preview
from vibecanvas_api.services.platform_mcp.interactive_tools.schema import interactive_view_json_schema


@pytest.mark.asyncio
async def test_workflow_preview_resolves_and_persists_a_version_reference(monkeypatch):
    from vibecanvas_api.services.agent_resources import workflow_transfer
    module = importlib.import_module("vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact")
    persist = AsyncMock()
    resolve = AsyncMock(return_value={"id": "wf", "name": "Saved flow", "version": "v2.sv3", "workflow": {"private": {}}})
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    monkeypatch.setattr(workflow_transfer, "read_workflow_snapshot", resolve)
    runtime = SimpleNamespace(context=SimpleNamespace())
    _, artifact = await render_preview.coroutine(type="workflow", runtime=runtime)
    resolve.assert_awaited_once_with(runtime.context, workflow_id="", version="")
    definition = artifact["payload"]["artifact"]
    assert definition["component_type"] == "workflow_preview"
    assert definition["props"] == {"workflow_id": "wf", "version": "v2.sv3", "description": ""}
    assert artifact["meta"]["workflow_preview"] == {"id": "wf", "version": "v2.sv3"}
    assert "private" not in json.dumps(artifact)
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_preview_does_not_persist_when_authorization_fails(monkeypatch):
    from vibecanvas_api.services.agent_resources import workflow_transfer
    module = importlib.import_module("vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact")
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    monkeypatch.setattr(workflow_transfer, "read_workflow_snapshot", AsyncMock(side_effect=ToolError("permission_denied", "Denied")))
    _, artifact = await render_preview.coroutine(type="workflow", source="private", runtime=SimpleNamespace(context=SimpleNamespace()))
    assert artifact["status"] == "error"
    persist.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,source", [
    ("file", "/data/report.pdf"),
    ("url", "https://example.com/docs"),
])
async def test_unified_preview_persists_both_types_without_waiting(monkeypatch, kind, source):
    module = importlib.import_module(
        "vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact"
    )
    persist = AsyncMock()
    prepare = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    monkeypatch.setattr(module, "_prepare_file_preview", prepare)
    runtime = SimpleNamespace(context=SimpleNamespace())
    content, artifact = await render_preview.coroutine(
        type=kind, source=source, title="Preview test",
        runtime=runtime,
    )
    assert artifact["status"] == "success"
    assert artifact["meta"]["tool"] == "render_preview"
    assert artifact["ref"].startswith("tool://render_preview/")
    definition = artifact["payload"]["artifact"]
    assert definition["component_type"] == f"{kind}_preview"
    assert definition["props"]["path" if kind == "file" else "url"] == source
    assert definition["preview"]["mode"] == "optional"
    assert definition["completion_mode"] == "render_only"
    assert "render_preview" in content
    persist.assert_awaited_once()
    if kind == "file":
        prepare.assert_awaited_once_with(runtime=runtime, path=source)
    else:
        prepare.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"type": "file", "source": "relative.pdf"},
    {"type": "file", "source": "/data/../secret"},
    {"type": "file", "source": "https://example.com"},
    {"type": "url", "source": "javascript:alert(1)"},
    {"type": "url", "source": "file:///data/report.pdf"},
    {"type": "url", "source": ""},
    {"type": "url", "source": "https://example.com", "file_type": "html"},
    {"type": "html", "source": "<div>test</div>"},
])
async def test_unified_preview_rejects_invalid_inputs_before_persist(monkeypatch, arguments):
    module = importlib.import_module(
        "vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact"
    )
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    _, artifact = await render_preview.coroutine(
        **arguments, runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    assert artifact["status"] == "error"
    assert artifact["error"]["code"] == "invalid_interactive_input"
    persist.assert_not_awaited()


def test_public_schema_is_flat_and_supports_all_preview_types():
    schema = render_preview.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"type", "source", "title", "file_type", "description", "version"}
    assert schema["required"] == ["type"]
    assert schema["properties"]["type"]["enum"] == ["file", "url", "workflow"]
    assert all(token not in json.dumps(schema) for token in ("oneOf", "anyOf", "discriminator"))
    assert "require_human_confirm" not in schema["properties"]


@pytest.mark.asyncio
async def test_url_preview_tool_publishes_isolated_webview_artifact(monkeypatch):
    module = importlib.import_module(
        "vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact"
    )
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)

    content, artifact = await render_preview.coroutine(
        type="url",
        title="Reference page",
        source="https://example.com/docs?section=preview",
        description="External documentation",
        runtime=SimpleNamespace(
            context=SimpleNamespace(
                tenant_id="tenant_1",
                chat_id="chat_1",
                turn_id="turn_1",
            )
        ),
    )

    definition = artifact["payload"]["artifact"]
    assert artifact["status"] == "success"
    assert definition["component_type"] == "url_preview"
    assert definition["props"] == {
        "url": "https://example.com/docs?section=preview",
        "description": "External documentation",
    }
    assert definition["height"] == 520
    assert "render_preview → url_preview" in content
    assert artifact["ref"].startswith("tool://render_preview/")
    assert artifact["meta"]["tool"] == "render_preview"
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_url_preview_rejects_non_http_navigation(monkeypatch):
    module = importlib.import_module(
        "vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact"
    )
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)

    content, artifact = await render_preview.coroutine(
        type="url",
        title="Unsafe page",
        source="javascript:alert(1)",
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert artifact["status"] == "error"
    assert artifact["error"]["code"] == "invalid_interactive_input"
    assert "absolute HTTP(S) URL" in content
    persist.assert_not_awaited()


def test_generated_frontend_contract_matches_backend_schema():
    root = Path(__file__).resolve().parents[3]
    generated = json.loads(
        (root / "web/src/components/agent-sidebar/tool-render/interactive-view-schema.generated.json").read_text()
    )
    assert generated == interactive_view_json_schema()


@pytest.mark.asyncio
async def test_invalid_path_is_an_agent_readable_tool_error():
    content, artifact = await render_preview.coroutine(
        type="file",
        source="relative/report.pdf",
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    assert "Fix these fields and call the tool again" in content
    assert artifact["status"] == "error"
    assert artifact["error"]["code"] == "invalid_interactive_input"
    assert artifact["artifact"]["kind"] == "tool_error"


@pytest.mark.asyncio
async def test_flat_file_preview_preserves_file_metadata(monkeypatch):
    module = importlib.import_module(
        "vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact"
    )

    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    content, artifact = await render_preview.coroutine(
        type="file",
        source="/mount/data/ecommerce-checkout-sequence.drawio",
        description="Checkout sequence",
        runtime=SimpleNamespace(
            context=SimpleNamespace(tenant_id="tenant_1", chat_id="chat_1", turn_id="turn_1")
        ),
    )
    definition = artifact["payload"]["artifact"]
    assert artifact["status"] == "success"
    assert definition["title"] == "ecommerce-checkout-sequence.drawio"
    assert definition["component_type"] == "file_preview"
    assert definition["props"] == {
        "path": "/mount/data/ecommerce-checkout-sequence.drawio",
        "file_type": "auto",
        "description": "Checkout sequence",
    }
    assert definition["completion_mode"] == "render_only"
    assert definition["interaction_schema"] == {}
    assert "render_preview → file_preview" in content
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_does_not_return_a_nonrecoverable_card_without_chat_context():
    content, artifact = await render_preview.coroutine(
        type="file",
        source="/mount/data/review.pdf",
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    assert "durable chat context" in content
    assert artifact["status"] == "error"
    assert artifact["error"]["code"] == "interactive_persistence_context_missing"


@pytest.mark.asyncio
async def test_database_failure_is_not_reported_as_a_successful_card(monkeypatch):
    from vibecanvas_api.storage import db

    def broken_session_scope(*, tenant_id: str):
        raise RuntimeError(f"database unavailable for {tenant_id}")

    monkeypatch.setattr(db, "session_scope", broken_session_scope)
    runtime = SimpleNamespace(
        context=SimpleNamespace(tenant_id="tenant_1", chat_id="chat_1", turn_id="turn_1")
    )

    with pytest.raises(ToolError, match="interactive_persistence_failed"):
        await _persist_interactive_state(
            runtime=runtime,
            artifact_id="ia_1",
            definition={
                "interaction_schema": {},
                "component_type": "html_preview",
                "completion_mode": "wait_for_submit",
            },
            component_type="html_preview",
            completion_mode="wait_for_submit",
            title="Dataset review",
            path=None,
            content_hash="sha256:test",
        )


@pytest.mark.asyncio
async def test_file_preview_defers_type_validation_to_preview(monkeypatch):
    module = importlib.import_module(
        "vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact"
    )
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    session = SimpleNamespace(sync_workspace_path=AsyncMock(return_value=True))
    content, artifact = await render_preview.coroutine(
        type="file",
        source="/data/diagrams/broken.drawio",
        title="Broken flow",
        runtime=SimpleNamespace(context=SimpleNamespace(_attached_session=session)),
    )

    assert artifact["status"] == "success"
    assert "render_preview → file_preview" in content
    assert artifact["payload"]["artifact"]["props"] == {
        "path": "/data/diagrams/broken.drawio",
        "file_type": "auto",
        "description": "",
    }
    persist.assert_awaited_once()
    session.sync_workspace_path.assert_awaited_once_with(
        "/data/diagrams/broken.drawio"
    )


@pytest.mark.asyncio
async def test_file_preview_preserves_optional_type_hint(monkeypatch):
    module = importlib.import_module(
        "vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact"
    )
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    session = SimpleNamespace(sync_workspace_path=AsyncMock(return_value=True))
    _, artifact = await render_preview.coroutine(
        type="file",
        source="/data/diagrams/flow.drawio",
        title="Interactive flow",
        file_type="drawio",
        runtime=SimpleNamespace(
            context=SimpleNamespace(
                tenant_id="tenant_1",
                chat_id="chat_1",
                turn_id="turn_1",
                _attached_session=session,
            )
        ),
    )

    assert artifact["status"] == "success"
    definition = artifact["payload"]["artifact"]
    assert definition["props"]["file_type"] == "drawio"
    assert "diagram_validation" not in artifact["payload"]
    session.sync_workspace_path.assert_awaited_once_with(
        "/data/diagrams/flow.drawio"
    )
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsynced_diagram_file_creates_no_interactive_card(monkeypatch):
    module = importlib.import_module(
        "vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact"
    )
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    session = SimpleNamespace(sync_workspace_path=AsyncMock(return_value=False))

    content, artifact = await render_preview.coroutine(
        type="file",
        source="/data/diagrams/unsynced.drawio",
        title="Unsynced flow",
        runtime=SimpleNamespace(context=SimpleNamespace(_attached_session=session)),
    )

    assert artifact["status"] == "error"
    assert artifact["error"]["code"] == "file_preview_sync_failed"
    assert "before creating its Preview" in content
    persist.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('html', [
    '<img src="https://images.example/photo.jpg">',
    '<picture><source srcset="//images.example/photo.jpg 2x"><img src="photo.jpg"></picture>',
    '<video poster="https://images.example/poster.jpg"></video>',
])
async def test_html_external_media_returns_actionable_error_before_publication(monkeypatch, html):
    module = importlib.import_module("vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact")
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    session = SimpleNamespace(read_file=AsyncMock(return_value={"ok": True, "content": html}),
                              sync_workspace_path=AsyncMock(return_value=True))
    content, artifact = await render_preview.coroutine(
        type="file", source="/data/report.html",
        runtime=SimpleNamespace(context=SimpleNamespace(_attached_session=session)),
    )
    assert artifact["error"]["code"] == "file_preview_external_media"
    assert "relative" in content
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_local_images_and_external_source_links_can_be_published(monkeypatch):
    module = importlib.import_module("vibecanvas_api.services.platform_mcp.interactive_tools.preview_artifact")
    persist = AsyncMock()
    monkeypatch.setattr(module, "_persist_interactive_state", persist)
    html = '<img src="photo.jpg"><img src="/data/photo.jpg"><img src="data:image/png;base64,AA=="><a href="https://example.com/source">Source</a>'
    session = SimpleNamespace(read_file=AsyncMock(return_value={"ok": True, "content": html}),
                              sync_workspace_path=AsyncMock(return_value=True))
    _, artifact = await render_preview.coroutine(
        type="file", source="/data/report.html",
        runtime=SimpleNamespace(context=SimpleNamespace(_attached_session=session)),
    )
    assert artifact["status"] == "success"
    persist.assert_awaited_once()
