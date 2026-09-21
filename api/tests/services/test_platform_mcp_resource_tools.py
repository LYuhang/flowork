from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime

from vibecanvas_api.services.platform_mcp.resource_tools import (
    deployment_collect_diagnostics,
    deployment_create,
    deployment_delete,
    deployment_list,
    deployment_update,
    knowledge_get,
    knowledge_create,
    knowledge_update,
    knowledge_list,
    knowledge_search,
    task_create_scheduled_run,
    task_collect_diagnostics,
    task_delete_scheduled_run,
    task_list,
    task_update_scheduled_run,
)
from vibecanvas_api.services.knowledge_packages import package_snapshot
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployment_invocations import (
    DeploymentInvocationsRepo,
)
from vibecanvas_api.storage.repo_tasks import TasksRepo


KNOWLEDGE_FORMAT_FIXTURES = {
    "README.md": b"# Format matrix\n\nAuthoritative package fixtures.",
    "docs/report.pdf": b"%PDF-1.7\nknowledge-pdf\n%%EOF",
    "slides/deck.pptx": b"PK\x03\x04knowledge-pptx",
    "images/diagram.png": b"\x89PNG\r\n\x1a\nknowledge-image",
    "media/brief.mp3": b"ID3knowledge-audio",
    "media/demo.mp4": b"\x00\x00\x00\x18ftypmp42knowledge-video",
    "notes/guide.md": b"# Guide\n\nKnowledge markdown.",
    "tables/metrics.csv": b"name,value\nalpha,1\n",
}

KNOWLEDGE_FORMAT_CONTRACT = {
    "README.md": ("text/markdown", "markdown", "pending"),
    "docs/report.pdf": ("application/pdf", "pdf", "pending"),
    "slides/deck.pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx",
        "pending",
    ),
    "images/diagram.png": ("image/png", "binary", "stored"),
    "media/brief.mp3": ("audio/mpeg", "binary", "stored"),
    "media/demo.mp4": ("video/mp4", "binary", "stored"),
    "notes/guide.md": ("text/markdown", "markdown", "pending"),
    "tables/metrics.csv": ("table/csv", "csv", "pending"),
}


async def _register(client) -> tuple[dict[str, str], dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"platform_mcp_{uuid.uuid4().hex[:12]}@example.com",
            "username": "Platform MCP",
            "password": "pw12345678",
        },
    )
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['session_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return headers, me


class _Sandbox:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})

    async def list_dir(self, path: str) -> dict:
        prefix = path.rstrip("/") + "/"
        entries: dict[str, dict] = {}
        for file_path, data in self.files.items():
            if not file_path.startswith(prefix):
                continue
            relative = file_path[len(prefix):]
            name, separator, _rest = relative.partition("/")
            entries[name] = {
                "name": name,
                "is_dir": bool(separator),
                "size": 0 if separator else len(data),
            }
        return {"ok": True, "entries": sorted(entries.values(), key=lambda item: item["name"])}

    async def read_bytes(self, path: str) -> dict:
        if path not in self.files:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "data": self.files[path]}

    async def write_bytes(self, path: str, data: bytes) -> dict:
        self.files[path] = data
        return {"ok": True, "bytes": len(data)}


def _runtime(me: dict, *, authorization_client, sandbox=None) -> ToolRuntime:
    async def sandbox_session():
        return sandbox

    return ToolRuntime(
        state={},
        context=SimpleNamespace(
            username=me["user_id"],
            tenant_id=me["tenant_id"],
            turn_id="resource-tools-turn",
            authorization_client=authorization_client,
            authorization_membership_id="resource-tools-membership",
            authorization_membership_role="owner",
            authorization_membership_status="active",
            authorization_session_generation=1,
            authorization_authentication_strength="test",
            sandbox_session=sandbox_session,
        ),
        config={"configurable": {"thread_id": "resource-tools"}},
        stream_writer=lambda _chunk: None,
        tool_call_id="resource-tools",
        store=None,
        tools=[],
    )


async def _workflow(client, headers: dict[str, str]) -> str:
    response = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Platform MCP workflow"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["wf_id"]


@pytest.mark.asyncio
async def test_knowledge_platform_mcp_uses_platform_database(
    client,
    monkeypatch,
) -> None:
    headers, me = await _register(client)
    created = await client.post(
        "/api/v1/kb",
        headers=headers,
        json={"name": "Platform knowledge"},
    )
    assert created.status_code == 201, created.text
    knowledge_base_id = created.json()["id"]
    sandbox = _Sandbox()
    runtime = _runtime(
        me,
        authorization_client=client._transport.app.state.openfga_client,
        sandbox=sandbox,
    )

    content, artifact = await knowledge_list.coroutine(
        runtime=runtime,
    )
    listed = json.loads(content)
    assert artifact["status"] == "success"
    assert [item["id"] for item in listed] == [knowledge_base_id]
    assert listed[0]["access"]["effective_role"] == "manager"

    content, _ = await knowledge_get.coroutine(
        kb_id=knowledge_base_id,
        runtime=runtime,
    )
    materialized = json.loads(content)
    assert materialized["id"] == knowledge_base_id
    assert materialized["package_version"] == 1
    assert materialized["readme"].endswith("/README.md")
    assert sandbox.files[materialized["readme"]].startswith(b"# Platform knowledge")

    content, _ = await knowledge_search.coroutine(
        kb_ids=[knowledge_base_id],
        query="anything",
        top_k=5,
        runtime=runtime,
    )
    results = json.loads(content)["results"]
    assert results == []


@pytest.mark.asyncio
async def test_knowledge_package_create_update_get_and_conflict(client) -> None:
    headers, me = await _register(client)
    sandbox = _Sandbox({
        "/data/new-package/README.md": b"# Evaluation\n\nPackage guide.",
        "/data/new-package/notes/findings.md": b"Initial finding.",
    })
    runtime = _runtime(
        me,
        authorization_client=client._transport.app.state.openfga_client,
        sandbox=sandbox,
    )
    with patch(
        "vibecanvas_api.services.platform_mcp.resource_tools.enqueue_package_indexing",
        new=AsyncMock(),
    ):
        content, _ = await knowledge_create.coroutine(
            name="Evaluation notes",
            description="Reusable evaluation research",
            source_path="/data/new-package",
            runtime=runtime,
        )
        created = json.loads(content)
        assert created["package_version"] == 1
        assert created["file_count"] == 2

        package_files = await client.get(
            f"/api/v1/kb/{created['id']}/files",
            headers=headers,
        )
        assert package_files.status_code == 200, package_files.text
        assert {
            item["name"]: (item["mime_type"], item["parser_type"])
            for item in package_files.json()
        } == {
            "README.md": ("text/markdown", "markdown"),
            "notes/findings.md": ("text/markdown", "markdown"),
        }

        sandbox.files["/data/new-package/notes/findings.md"] = b"Updated finding."
        content, _ = await knowledge_update.coroutine(
            kb_id=created["id"],
            source_path="/data/new-package",
            expected_version=1,
            runtime=runtime,
        )
        updated = json.loads(content)
        assert updated["package_version"] == 2

        with pytest.raises(RuntimeError, match="knowledge_version_conflict"):
            await knowledge_update.coroutine(
                kb_id=created["id"],
                source_path="/data/new-package",
                expected_version=1,
                runtime=runtime,
            )

        content, _ = await knowledge_get.coroutine(
            kb_id=created["id"],
            destination_path="/data/reopened",
            runtime=runtime,
        )
        reopened = json.loads(content)
        assert reopened["package_version"] == 2
        assert sandbox.files["/data/reopened/notes/findings.md"] == b"Updated finding."


@pytest.mark.asyncio
async def test_knowledge_format_matrix_create_update_snapshot_and_raw_preview(
    client,
) -> None:
    """Known office/media/text types keep canonical MIME and parser status."""
    headers, me = await _register(client)
    source_root = "/data/format-matrix"
    sandbox = _Sandbox({
        f"{source_root}/{path}": data
        for path, data in KNOWLEDGE_FORMAT_FIXTURES.items()
    })
    runtime = _runtime(
        me,
        authorization_client=client._transport.app.state.openfga_client,
        sandbox=sandbox,
    )
    with patch(
        "vibecanvas_api.services.platform_mcp.resource_tools.enqueue_package_indexing",
        new=AsyncMock(),
    ):
        content, _ = await knowledge_create.coroutine(
            name="Format matrix",
            source_path=source_root,
            runtime=runtime,
        )
        created = json.loads(content)
        assert created["package_version"] == 1

        listed = await client.get(
            f"/api/v1/kb/{created['id']}/files",
            headers=headers,
        )
        assert listed.status_code == 200, listed.text
        rows = {item["name"]: item for item in listed.json()}
        assert {
            path: (row["mime_type"], row["parser_type"], row["status"])
            for path, row in rows.items()
        } == KNOWLEDGE_FORMAT_CONTRACT

        for path, expected_bytes in KNOWLEDGE_FORMAT_FIXTURES.items():
            raw = await client.get(
                f"/api/v1/kb/{created['id']}/files/{rows[path]['id']}/raw",
                headers=headers,
            )
            assert raw.status_code == 200, (path, raw.text)
            assert raw.content == expected_bytes
            assert raw.headers["content-type"].split(";", 1)[0] == (
                KNOWLEDGE_FORMAT_CONTRACT[path][0]
            )

        revision_two = {
            path: data + b"\nrevision-two"
            for path, data in KNOWLEDGE_FORMAT_FIXTURES.items()
        }
        sandbox.files = {
            f"{source_root}/{path}": data
            for path, data in revision_two.items()
        }
        updated_content, _ = await knowledge_update.coroutine(
            kb_id=created["id"],
            source_path=source_root,
            expected_version=1,
            runtime=runtime,
        )
        assert json.loads(updated_content)["package_version"] == 2

        updated_list = await client.get(
            f"/api/v1/kb/{created['id']}/files",
            headers=headers,
        )
        assert updated_list.status_code == 200, updated_list.text
        assert {
            item["name"]: (
                item["mime_type"],
                item["parser_type"],
                item["status"],
            )
            for item in updated_list.json()
        } == KNOWLEDGE_FORMAT_CONTRACT

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        snapshot = await package_snapshot(session, uuid.UUID(created["id"]))
    assert {
        item.path: (item.content_type, item.data)
        for item in snapshot
    } == {
        path: (KNOWLEDGE_FORMAT_CONTRACT[path][0], data)
        for path, data in revision_two.items()
    }


@pytest.mark.asyncio
async def test_knowledge_create_validates_before_persisting_resource(client) -> None:
    headers, me = await _register(client)
    sandbox = _Sandbox({
        "/data/invalid-package/notes/findings.md": b"Missing root README.",
    })
    runtime = _runtime(
        me,
        authorization_client=client._transport.app.state.openfga_client,
        sandbox=sandbox,
    )

    with pytest.raises(ValueError, match="README.md"):
        await knowledge_create.coroutine(
            name="Must not persist",
            source_path="/data/invalid-package",
            runtime=runtime,
        )

    listed = await client.get("/api/v1/kb", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.asyncio
async def test_task_platform_mcp_crud_uses_platform_database(client) -> None:
    headers, me = await _register(client)
    workflow_id = await _workflow(client, headers)
    sandbox = _Sandbox()
    runtime = _runtime(
        me,
        authorization_client=client._transport.app.state.openfga_client,
        sandbox=sandbox,
    )

    content, artifact = await task_create_scheduled_run.coroutine(
        name="Hourly review",
        workflow_id=workflow_id,
        schedule_type="interval",
        interval_seconds=3600,
        require_user_auth=True,
        runtime=runtime,
    )
    created = json.loads(content)
    task_id = created["task"]["id"]
    assert artifact["status"] == "success"
    assert created["schedule"]["name"] == "Hourly review"

    content, _ = await task_update_scheduled_run.coroutine(
        task_id=task_id,
        name="Daily review",
        enabled=False,
        require_user_auth=True,
        runtime=runtime,
    )
    assert json.loads(content)["schedule"]["name"] == "Daily review"

    content, _ = await task_list.coroutine(
        workflow_id=workflow_id,
        limit=20,
        offset=0,
        runtime=runtime,
    )
    listed = json.loads(content)
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == task_id
    assert listed["has_more"] is False

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        await TasksRepo(session).insert_event(
            uuid.UUID(task_id),
            "log",
            {
                "level": "error",
                "message": "diagnostic marker",
                "error": {"code": "workflow_timeout"},
            },
            uuid.UUID(me["tenant_id"]),
        )

    content, _ = await task_collect_diagnostics.coroutine(
        task_id=task_id,
        event_limit=20,
        runtime=runtime,
    )
    diagnostics = json.loads(content)
    assert diagnostics["statistics"]["event_counts"]["log"] == 1
    assert diagnostics["files"]["summary.json"].endswith("/summary.json")
    events = sandbox.files[diagnostics["files"]["events.jsonl"]].decode()
    assert "diagnostic marker" in events
    assert "executions.jsonl" in diagnostics["files"]

    content, _ = await task_delete_scheduled_run.coroutine(
        task_id=task_id,
        require_user_auth=True,
        runtime=runtime,
    )
    assert json.loads(content) == {"status": "deleted"}


@pytest.mark.asyncio
async def test_deployment_platform_mcp_crud_uses_platform_database(client) -> None:
    headers, me = await _register(client)
    workflow_id = await _workflow(client, headers)
    sandbox = _Sandbox()
    runtime = _runtime(
        me,
        authorization_client=client._transport.app.state.openfga_client,
        sandbox=sandbox,
    )
    slug = f"mcp-{uuid.uuid4().hex[:12]}"

    content, artifact = await deployment_create.coroutine(
        workflow_id=workflow_id,
        name="Agent deployment",
        slug=slug,
        trigger_type="api",
        version_pin="head",
        require_user_auth=True,
        runtime=runtime,
    )
    created = json.loads(content)
    deployment_id = created["id"]
    assert artifact["status"] == "success"
    assert created["api_key"]

    content, _ = await deployment_update.coroutine(
        deployment_id=deployment_id,
        name="Updated deployment",
        enabled=False,
        require_user_auth=True,
        runtime=runtime,
    )
    assert json.loads(content)["name"] == "Updated deployment"

    content, _ = await deployment_list.coroutine(
        workflow_id=workflow_id,
        limit=20,
        offset=0,
        runtime=runtime,
    )
    listed = json.loads(content)
    assert listed["items"][0]["id"] == deployment_id
    assert "api_key_hash" not in listed["items"][0]

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        repo = DeploymentInvocationsRepo(session)
        invocation_id = await repo.create(
            tenant_id=uuid.UUID(me["tenant_id"]),
            deployment_id=uuid.UUID(deployment_id),
            wf_id=workflow_id,
            trigger_type="api",
            source="sync_api",
            status="running",
        )
        await repo.mark_terminal(
            invocation_id,
            status="failed",
            latency_ms=125.0,
            error="workflow_timeout",
        )

    now = datetime.now(timezone.utc)
    content, _ = await deployment_collect_diagnostics.coroutine(
        deployment_id=deployment_id,
        from_time=now - timedelta(hours=1),
        to_time=now + timedelta(minutes=1),
        invocation_limit=20,
        runtime=runtime,
    )
    diagnostics = json.loads(content)
    assert diagnostics["statistics"]["calls"] == 1
    assert diagnostics["statistics"]["errors"] == 1
    invocations = sandbox.files[
        diagnostics["files"]["invocations.jsonl"]
    ].decode()
    assert "workflow_timeout" in invocations

    content, _ = await deployment_delete.coroutine(
        deployment_id=deployment_id,
        require_user_auth=True,
        runtime=runtime,
    )
    assert json.loads(content) == {"status": "deleted"}
