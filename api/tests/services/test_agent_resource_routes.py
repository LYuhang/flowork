from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from vibecanvas_api.routes import deployments, kb
from vibecanvas_api.services.agent_runtime import cli_deployments, cli_knowledge
from vibecanvas_api.services.agent_runtime.resource_routes import resource_route_params
from vibecanvas_api.services.knowledge_packages import (
    PackageFile, package_snapshot, replace_package, validate_package,
)
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
            "email": f"agent_cli_{uuid.uuid4().hex[:12]}@example.com",
            "username": "Agent CLI",
            "password": "pw12345678",
        },
    )
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['session_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return headers, me


def _context(me: dict, *, authorization_client):
    return SimpleNamespace(
        username=me["user_id"],
        tenant_id=me["tenant_id"],
        turn_id="resource-cli-turn",
        authorization_client=authorization_client,
        authorization_membership_id="resource-cli-membership",
        authorization_membership_role="owner",
        authorization_membership_status="active",
        authorization_session_generation=1,
        authorization_authentication_strength="test",
    )


async def _publish(context, files, *, name=None, knowledge_id=None):
    """Exercise shared route policies and package storage without a retired Tool.

    Full CLI approval/live-run fencing is covered by test_knowledge_cli. These
    integration tests retain the resource/format contract of the former adapters.
    """
    package = validate_package([PackageFile(path, data, "") for path, data in files.items()])
    async with session_scope(tenant_id=context.tenant_id) as session:
        params = resource_route_params(context, session)
        if knowledge_id is None:
            await cli_knowledge.authorize(params, None, kb.Action.CREATE)
            row = await kb._create_knowledge_package(
                body=kb.KbCreate(name=name), package_files=package, derive_index=True, **params,
            )
            return {"id": row.id, "package_version": row.package_version, "file_count": len(package)}
        identifier = uuid.UUID(knowledge_id)
        await cli_knowledge.authorize(params, identifier, kb.Action.UPDATE)
        number, _ = await replace_package(
            session, kb_id=identifier, actor_user_id=uuid.UUID(context.username),
            expected_version=None, files=package,
        )
        return {"id": knowledge_id, "package_version": number, "file_count": len(package)}


async def _workflow(client, headers: dict[str, str]) -> str:
    response = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Agent CLI workflow"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["wf_id"]


@pytest.mark.asyncio
async def test_knowledge_cli_reads_authorized_database(
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
    context = _context(
        me,
        authorization_client=client._transport.app.state.openfga_client,
    )
    listed = await cli_knowledge.read(context, "knowledge.list", {"offset": 0, "limit": 20})
    assert listed["status"] == "succeeded"
    assert [item["knowledge_id"] for item in listed["knowledge"]] == [knowledge_base_id]
    materialized = await cli_knowledge.read(context, "knowledge.download", {"knowledge_id": knowledge_base_id})
    assert materialized["knowledge_id"] == knowledge_base_id
    assert materialized["package_version"] == 1
    readme = next(item for item in materialized["files"] if item["path"] == "README.md")
    assert base64.b64decode(readme["data"]).startswith(b"# Platform knowledge")
    result = await cli_knowledge.read(context, "knowledge.search", {"knowledge_id": [knowledge_base_id], "query": "anything", "limit": 5})
    assert result["results"] == []
    _, stranger = await _register(client)
    other = _context(stranger, authorization_client=context.authorization_client)
    assert (await cli_knowledge.read(other, "knowledge.list", {"offset": 0, "limit": 20}))["knowledge"] == []
    with pytest.raises(HTTPException):
        await cli_knowledge.read(other, "knowledge.download", {"knowledge_id": knowledge_base_id})


@pytest.mark.asyncio
async def test_knowledge_package_create_upload_download_uses_latest_version(client) -> None:
    headers, me = await _register(client)
    files = {"README.md": b"# Evaluation\n\nPackage guide.", "notes/findings.md": b"Initial finding."}
    context = _context(
        me,
        authorization_client=client._transport.app.state.openfga_client,
    )
    with patch(
        "vibecanvas_api.routes.kb.enqueue_package_indexing",
        new=AsyncMock(),
    ):
        created = await _publish(context, files, name="Evaluation notes")
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

        files["notes/findings.md"] = b"Updated finding."
        updated = await _publish(context, files, knowledge_id=created["id"])
        assert updated["package_version"] == 2
        # No expected_version parameter: another successful upload creates v3.
        repeated = await _publish(context, files, knowledge_id=created["id"])
        assert repeated["package_version"] == 3
        reopened = await cli_knowledge.read(context, "knowledge.download", {"knowledge_id": created["id"]})
        assert reopened["package_version"] == 3
        downloaded = {item["path"]: base64.b64decode(item["data"]) for item in reopened["files"]}
        assert downloaded["notes/findings.md"] == b"Updated finding."


@pytest.mark.asyncio
async def test_knowledge_format_matrix_create_update_snapshot_and_raw_preview(
    client,
) -> None:
    """Known office/media/text types keep canonical MIME and parser status."""
    headers, me = await _register(client)
    context = _context(
        me,
        authorization_client=client._transport.app.state.openfga_client,
    )
    with patch(
        "vibecanvas_api.routes.kb.enqueue_package_indexing",
        new=AsyncMock(),
    ):
        created = await _publish(context, KNOWLEDGE_FORMAT_FIXTURES, name="Format matrix")
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
        updated = await _publish(context, revision_two, knowledge_id=created["id"])
        assert updated["package_version"] == 2

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
    context = _context(
        me,
        authorization_client=client._transport.app.state.openfga_client,
    )

    with pytest.raises(ValueError, match="README.md"):
        await _publish(context, {"notes/findings.md": b"Missing root README."}, name="Must not persist")

    listed = await client.get("/api/v1/kb", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.asyncio
async def test_task_cli_reads_and_diagnostics_use_authorized_database(client) -> None:
    from vibecanvas_api.services.agent_runtime.cli_tasks import _read
    headers, me = await _register(client)
    workflow_id = await _workflow(client, headers)
    context = _context(me, authorization_client=client._transport.app.state.openfga_client)
    created = await client.post("/api/v1/tasks/scheduled-runs", headers=headers,
        json={"name": "CLI query fixture", "workflow_id": workflow_id,
              "major": "v1", "enabled": False, "schedule_type": "interval", "interval_seconds": 3600})
    assert created.status_code == 201, created.text
    task_id = created.json()["task"]["id"]
    emit = AsyncMock()
    listed = await _read(context, "task.list", {"workflow_id": workflow_id}, emit)
    assert listed["tasks"][0]["task_id"] == task_id
    assert listed["tasks"][0]["task_type"] == "schedule_run"
    detail = await _read(context, "task.history", {"task_id": task_id, "task_type": "schedule_run"}, emit)
    assert detail["schedule"]["workflow_selector"] == {"major": "v1"}
    assert detail["task_id"] == task_id
    assert detail["plan_status"] == "paused"
    assert detail["history"] == []
    assert "id" not in detail and "id" not in detail["schedule"]
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        repo = TasksRepo(session)
        schedule = await repo.get_schedule_by_task(uuid.UUID(task_id))
        execution_id = uuid.uuid4()
        await repo.create_scheduled_execution(execution_id=execution_id, tenant_id=uuid.UUID(me["tenant_id"]),
            schedule_id=schedule.id, workflow_id=workflow_id, run_key="cli-export", trigger_type="manual", input_snapshot={})
        await TasksRepo(session).insert_event(uuid.UUID(task_id), "log",
            {"message": "diagnostic marker", "data": {"execution_id": str(execution_id)}}, uuid.UUID(me["tenant_id"]))
        await repo.insert_event(uuid.UUID(task_id), "log",
            {"message": "different execution", "data": {"execution_id": str(uuid.uuid4())}}, uuid.UUID(me["tenant_id"]))
    target = {"task_id": task_id, "task_type": "schedule_run", "execution_id": str(execution_id)}
    diagnostics = await _read(context, "task.logs", {**target, "export": True}, emit)
    summary = json.loads(diagnostics["files"]["status.json"])
    assert summary["execution_id"] == str(execution_id)
    assert "diagnostic marker" in diagnostics["files"]["logs.jsonl"]
    assert "different execution" not in diagnostics["files"]["logs.jsonl"]
    assert "history.jsonl" in diagnostics["files"]
    selected = await _read(context, "task.status", target, emit)
    assert selected["execution_id"] == str(execution_id)
    from vibecanvas_api.agents.tools.decorator import ToolError
    with pytest.raises(ToolError, match="invalid_arguments"):
        await _read(context, "task.status", {"task_id": task_id, "task_type": "schedule_run"}, emit)
    with pytest.raises(ToolError, match="wrong_task_type"):
        await _read(context, "task.status", {"task_id": task_id, "task_type": "batch_exec"}, emit)
    _, stranger = await _register(client)
    other = _context(stranger, authorization_client=client._transport.app.state.openfga_client)
    assert (await _read(other, "task.list", {}, emit))["tasks"] == []
    with pytest.raises(HTTPException):
        await _read(other, "task.status", target, emit)


@pytest.mark.asyncio
@pytest.mark.parametrize("mount", [False, True])
async def test_batch_creation_persists_mount_and_delete_guards_live_execution(client, mount):
    headers, me = await _register(client)
    workflow_id = await _workflow(client, headers)
    created = await client.post(f"/api/v1/workflows/{workflow_id}/batch", headers=headers,
        json={"data_source": {"rows": [{}]}, "column_mapping": {}, "major": "v1", "mount_enabled": mount})
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert detail.json()["payload"]["mount_enabled"] is mount
    assert (await client.delete(f"/api/v1/tasks/{task_id}", headers=headers)).status_code == 409
    outsider, _ = await _register(client)
    assert (await client.delete(f"/api/v1/tasks/{task_id}", headers=outsider)).status_code in {403, 404}
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        await TasksRepo(session).update_status(uuid.UUID(task_id), status="finished")
    deleted = await client.delete(f"/api/v1/tasks/{task_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).status_code in {403, 404}
    assert (await client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_schedule_json_download_requires_owned_terminal_execution(client):
    headers, me = await _register(client)
    workflow_id = await _workflow(client, headers)
    created = await client.post("/api/v1/tasks/scheduled-runs", headers=headers,
        json={"name": "Download fixture", "workflow_id": workflow_id, "major": "v1",
              "enabled": False, "schedule_type": "interval", "interval_seconds": 3600})
    assert created.status_code == 201, created.text
    task_id, execution_id = created.json()["task"]["id"], uuid.uuid4()
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        repo = TasksRepo(session)
        schedule = await repo.get_schedule_by_task(uuid.UUID(task_id))
        await repo.create_scheduled_execution(execution_id=execution_id, tenant_id=uuid.UUID(me["tenant_id"]),
            schedule_id=schedule.id, workflow_id=workflow_id, run_key="download", trigger_type="manual", input_snapshot={})
    url = f"/api/v1/tasks/scheduled-runs/{task_id}/executions/{execution_id}/download"
    assert (await client.get(url, headers=headers)).status_code == 409
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        await TasksRepo(session).update_scheduled_execution(execution_id, status="succeeded", result={"answer": 0, "flag": False})
    response = await client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"answer": 0, "flag": False}
    assert response.json()["execution_id"] == str(execution_id)
    outsider, _ = await _register(client)
    assert (await client.get(url, headers=outsider)).status_code in {403, 404}
    assert (await client.get(url.replace(str(execution_id), str(uuid.uuid4())), headers=headers)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["batch", "schedule"])
async def test_task_submission_reports_committed_id_when_projection_is_unavailable(client, monkeypatch, kind):
    from vibecanvas_api.routes import tasks, workflows
    headers, me = await _register(client)
    workflow_id = await _workflow(client, headers)
    module = workflows if kind == "batch" else tasks
    monkeypatch.setattr(module, "apply_committed_structural_mutations", AsyncMock(side_effect=RuntimeError("projection unavailable")))
    if kind == "batch":
        response = await client.post(f"/api/v1/workflows/{workflow_id}/batch", headers=headers,
            json={"data_source": {"rows": [{}]}, "column_mapping": {}, "major": "v1"})
    else:
        response = await client.post("/api/v1/tasks/scheduled-runs", headers=headers,
            json={"name": "projection fixture", "workflow_id": workflow_id, "major": "v1",
                  "enabled": False, "schedule_type": "interval", "interval_seconds": 3600})
    assert response.status_code in {200, 201, 202}, response.text
    result = response.json()
    assert result["authorization_pending"] is True
    task_id = result["task_id"] if kind == "batch" else result["task"]["id"]
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        task = await TasksRepo(session).get(uuid.UUID(task_id))
        assert task is not None
        assert task.workflow_id == workflow_id


@pytest.mark.asyncio
async def test_deployment_resource_routes_and_cli_diagnostics_use_authorized_database(client) -> None:
    headers, me = await _register(client)
    workflow_id = await _workflow(client, headers)
    context = _context(
        me,
        authorization_client=client._transport.app.state.openfga_client,
    )
    slug = f"cli-{uuid.uuid4().hex[:12]}"
    async with session_scope(tenant_id=context.tenant_id) as session:
        params = resource_route_params(context, session)
        created = await deployments.create_deployment(
            deployments.CreateDeploymentBody(wf_id=workflow_id, name="Agent deployment",
                slug=slug, trigger_type="api", version_pin="major", pinned_major=1),
            **params, _step_up=params["ctx"],
        )
    deployment_id = created["id"]
    assert created["api_key"]
    async with session_scope(tenant_id=context.tenant_id) as session:
        updated = await deployments.patch_deployment(
            uuid.UUID(deployment_id), deployments.PatchDeploymentBody(name="Updated deployment", enabled=False),
            **resource_route_params(context, session),
        )
    assert updated["name"] == "Updated deployment"
    listed = await cli_deployments.read(context, "deployment.list", {"workflow_id": workflow_id, "limit": 20, "offset": 0})
    assert listed["deployments"][0]["deployment_id"] == deployment_id
    assert "api_key_hash" not in listed["deployments"][0]

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
    diagnostics = await cli_deployments.read(context, "deployment.history", {
        "deployment_id": deployment_id, "from_time": (now - timedelta(hours=1)).isoformat(),
        "to_time": (now + timedelta(minutes=1)).isoformat(), "limit": 20, "export": True,
    })
    metrics = json.loads(diagnostics["files"]["metrics.json"])
    assert sum(item["calls"] for item in metrics["series"]) == 1
    assert sum(item["errors"] for item in metrics["series"]) == 1
    assert "workflow_timeout" in diagnostics["files"]["invocations.jsonl"]
    _, stranger = await _register(client)
    other = _context(stranger, authorization_client=context.authorization_client)
    assert (await cli_deployments.read(other, "deployment.list", {}))["deployments"] == []
    with pytest.raises(HTTPException):
        await cli_deployments.read(other, "deployment.status", {"deployment_id": deployment_id})
    async with session_scope(tenant_id=context.tenant_id) as session:
        await deployments.delete_deployment(uuid.UUID(deployment_id), **resource_route_params(context, session))
    assert (await client.get(f"/api/v1/deployments/{deployment_id}", headers=headers)).status_code == 404
