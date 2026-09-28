from __future__ import annotations

import uuid

import pytest

from vibecanvas_api.routes.storage import _task_artifact_size


async def _register(client) -> tuple[str, str]:
    email = f"storage_{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Storage Test", "password": "pw12345678"},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body["session_token"], body["user"]["user_id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mount_scope_id(user_id: str) -> str:
    return f"__mount_{user_id.replace('-', '')[:24]}"


def test_task_artifact_size_uses_persisted_plaintext_metadata():
    summary = {
        "artifact_sizes": {"csv": 1024, "jsonl": 2048, "summary": 512},
    }
    assert _task_artifact_size(summary, "results.csv") == 1024
    assert _task_artifact_size(summary, "results.jsonl") == 2048
    assert _task_artifact_size(summary, "summary.json") == 512
    assert _task_artifact_size(summary, "unknown.bin") is None
    assert _task_artifact_size({"artifact_sizes": {"csv": -1}}, "results.csv") is None


async def _create_workflow(client, token: str) -> str:
    r = await client.post(
        "/api/v1/workflows",
        json={"name": "storage workflow"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["wf_id"]


@pytest.mark.asyncio
async def test_storage_mount_text_crud(client):
    token, _user_id = await _register(client)
    headers = _auth(token)

    write = await client.put(
        "/api/v1/storage/content",
        json={
            "path": "/mount/docs/a.txt",
            "content": "hello mount",
            "content_type": "text/plain",
        },
        headers=headers,
    )
    assert write.status_code == 200, write.text
    assert write.json()["path"] == "/mount/docs/a.txt"

    read = await client.get(
        "/api/v1/storage/content",
        params={"path": "/mount/docs/a.txt"},
        headers=headers,
    )
    assert read.status_code == 200, read.text
    assert read.json()["content"] == "hello mount"

    listed = await client.get(
        "/api/v1/storage/list",
        params={"path": "/mount/docs"},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert [item["name"] for item in listed.json()["items"]] == ["a.txt"]

    renamed = await client.post(
        "/api/v1/storage/rename",
        json={"old_path": "/mount/docs/a.txt", "new_path": "/mount/docs/b.txt"},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["path"] == "/mount/docs/b.txt"

    deleted = await client.delete(
        "/api/v1/storage",
        params={"path": "/mount/docs/b.txt"},
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == 1


@pytest.mark.asyncio
async def test_storage_mount_mkdir_and_invalid_path(client):
    token, _user_id = await _register(client)
    headers = _auth(token)

    mkdir = await client.post(
        "/api/v1/storage/mkdir",
        json={"path": "/mount/empty"},
        headers=headers,
    )
    assert mkdir.status_code == 200, mkdir.text

    listed = await client.get(
        "/api/v1/storage/list",
        params={"path": "/mount"},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert any(item["name"] == "empty" and item["kind"] == "folder" for item in listed.json()["items"])

    bad = await client.get(
        "/api/v1/storage/list",
        params={"path": "/mount/../secret"},
        headers=headers,
    )
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_vfs_mount_scope_uses_same_user_vfs_namespace(client):
    token, user_id = await _register(client)
    headers = _auth(token)
    mount_scope = _mount_scope_id(user_id)

    write = await client.put(
        "/api/v1/vfs/content",
        json={
            "wf_id": mount_scope,
            "path": "/mount/from-vfs.txt",
            "content": "visible via storage",
            "content_type": "text/plain",
        },
        headers=headers,
    )
    assert write.status_code == 200, write.text

    listed = await client.get(
        "/api/v1/storage/list",
        params={"path": "/mount"},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert any(item["name"] == "from-vfs.txt" for item in listed.json()["items"])

    wrong_prefix = await client.put(
        "/api/v1/vfs/content",
        json={
            "wf_id": mount_scope,
            "path": "/data/not-allowed.txt",
            "content": "nope",
            "content_type": "text/plain",
        },
        headers=headers,
    )
    assert wrong_prefix.status_code == 400


@pytest.mark.asyncio
async def test_storage_workflow_root_is_present_when_user_has_no_workflows(client):
    token, _user_id = await _register(client)
    headers = _auth(token)

    roots = await client.get(
        "/api/v1/storage/list",
        params={"path": "/"},
        headers=headers,
    )
    assert roots.status_code == 200, roots.text
    assert [item["name"] for item in roots.json()["items"]] == [
        "mount",
        "project",
        "task",
        "workflow",
    ]

    workflows = await client.get(
        "/api/v1/storage/list",
        params={"path": "/workflow"},
        headers=headers,
    )
    assert workflows.status_code == 200, workflows.text
    assert workflows.json()["items"] == []
    assert workflows.json()["readonly"] is True


@pytest.mark.asyncio
async def test_storage_workflow_root_lists_owned_workflows_and_fixed_folders(client):
    token, _user_id = await _register(client)
    headers = _auth(token)
    wf_id = await _create_workflow(client, token)

    workflows = await client.get(
        "/api/v1/storage/list",
        params={"path": "/workflow"},
        headers=headers,
    )
    assert workflows.status_code == 200, workflows.text
    assert [
        (item["name"], item["path"], item["kind"])
        for item in workflows.json()["items"]
    ] == [(wf_id, f"/workflow/{wf_id}", "folder")]

    workflow = await client.get(
        "/api/v1/storage/list",
        params={"path": f"/workflow/{wf_id}"},
        headers=headers,
    )
    assert workflow.status_code == 200, workflow.text
    assert [item["name"] for item in workflow.json()["items"]] == [
        "data",
        "logs",
        "memory",
    ]
    assert workflow.json()["readonly"] is True
