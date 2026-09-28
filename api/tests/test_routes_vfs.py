"""VFS 2c read-only routes — list + content, stale, no-touch, 404.

Auth: registers a real user via /api/v1/auth/register (the dev-token
shortcut is not wired in this build, so we use the same real-session
pattern as test_business_route_auth). VFS rows are seeded directly into
that user's tenant (discovered from the workflow they create) so the
route — running under tenant_db RLS — can see them.

Uses the conftest async ``client`` (httpx + ASGITransport, no lifespan;
the autouse ``_migrate`` fixture has already created the schema) so the
async route + async seeding share one event loop.
"""
from __future__ import annotations

import uuid
import base64

import pytest
from sqlalchemy import text


async def _register(client) -> str:
    """Register a fresh user, return its bearer session token."""
    email = f"u{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post("/api/v1/auth/register",
                          json={"email": email, "username": "Test User", "password": "pw12345678"})
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_wf(client, token: str) -> str:
    r = await client.post("/api/v1/workflows", json={"name": "w"}, headers=_hdr(token))
    assert r.status_code == 201, r.text
    return r.json()["wf_id"]


async def _workspace_scopes(client, token: str, chat_id: str = "chat_vfs") -> tuple[str, str]:
    headers = _hdr(token)
    project = await client.post("/api/v1/projects", headers=headers, json={"name": "VFS tests"})
    assert project.status_code == 201, project.text
    carrier = (await client.get("/api/v1/chats/bootstrap", headers=headers)).json()["carrier_scope_id"]
    created = await client.put(f"/api/v1/chat-scopes/{carrier}/chats/{chat_id}", headers=headers,
                               json={"project_id": project.json()["project_id"]})
    assert created.status_code == 200, created.text
    r = await client.get(
        f"/api/v1/chats/workspace?chat_id={chat_id}", headers=_hdr(token))
    assert r.status_code == 200, r.text
    body = r.json()
    return body["workspace_scope_id"], body["mount_scope_id"]


async def _dev_tenant(pg_engine, wf_id: str) -> str:
    # pg_engine is the superuser role (bypasses RLS) so it can read the
    # workflow's tenant_id without an app.tenant_id GUC set.
    async with pg_engine.connect() as c:
        row = (await c.execute(
            text("SELECT tenant_id FROM workflows WHERE wf_id=:w"), {"w": wf_id})).one()
    return str(row[0])


async def _seed_vfs(app_engine, tenant: str, wf_id: str, *, path, content,
                    content_type="text/plain", wf_version=None, size_bytes=None):
    """Seed an artifact row in the post-unification (object-backed) shape: the
    `content` column was dropped, so bytes live in the ObjectStore at object_key
    and the row is metadata-only. `size_bytes` may override the real byte length
    (some tests assert a specific declared size)."""
    from vibecanvas_api.services.object_store import get_object_store
    key = f"artifacts/{tenant}/{wf_id}{path}"
    data = content.encode()
    get_object_store().put_bytes(key, data, content_type)
    async with app_engine.connect() as c:
        await c.execute(text("SELECT set_config('app.tenant_id',:t,false)"), {"t": tenant})
        await c.execute(text(
            "INSERT INTO vfs_artifacts(scope_id,path,object_key,content_type,wf_version,size_bytes) "
            "VALUES (:w,:p,:k,:ct,:v,:s)"),
            {"w": wf_id, "p": path, "k": key, "ct": content_type,
             "v": wf_version,
             "s": size_bytes if size_bytes is not None else len(data)})
        await c.commit()


async def _seed_vfs_object_text(app_engine, tenant: str, wf_id: str, *, path,
                                content, content_type="text/plain"):
    """Seed an OBJECT-backed text row (object_key set, content="", bytes in the
    process-singleton ObjectStore) — the post-unification on-disk shape. The
    route's read() must fetch_bytes + decode this back to text."""
    from vibecanvas_api.services.object_store import get_object_store
    key = f"artifacts/{tenant}/{wf_id}{path}"
    data = content.encode()
    get_object_store().put_bytes(key, data, content_type)
    async with app_engine.connect() as c:
        await c.execute(text("SELECT set_config('app.tenant_id',:t,false)"), {"t": tenant})
        await c.execute(text(
            "INSERT INTO vfs_artifacts(scope_id,path,object_key,content_type,size_bytes) "
            "VALUES (:w,:p,:k,:ct,:s)"),
            {"w": wf_id, "p": path, "k": key, "ct": content_type, "s": len(data)})
        await c.commit()


@pytest.mark.asyncio
async def test_content_route_object_backed_text_returns_decoded(client, app_engine, pg_engine):
    """Regression (CRITICAL-1): a NEW object-backed text artifact (content="",
    bytes in the store) read via /content returns the DECODED text — the route
    repo must be constructed with an object_store or this 500s."""
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs_object_text(app_engine, t, wf_id, path="/files/obj_1.txt",
                                content="hello object", content_type="text/plain")
    r = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/files/obj_1.txt",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "hello object"   # fetch_bytes + decode roundtrip
    assert body["content_type"] == "text/plain"


@pytest.mark.asyncio
async def test_list_returns_meta_without_content_and_stale_flag(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)   # workflow created at active v1.sv0
    await _seed_vfs(app_engine, t, wf_id, path="/data/fresh_1.jsonl",
                    content='{"a":1}', content_type="table/jsonl", wf_version="v1.sv0")
    await _seed_vfs(app_engine, t, wf_id, path="/data/old_1.jsonl",
                    content='{"a":1}', content_type="table/jsonl", wf_version="v0.sv0")

    r = await client.get(f"/api/v1/vfs?wf_id={wf_id}", headers=_hdr(tok))
    assert r.status_code == 200, r.text
    entries = {e["path"]: e for e in r.json()["entries"]}
    assert "content" not in entries["/data/fresh_1.jsonl"]
    assert entries["/data/fresh_1.jsonl"]["stale"] is False
    assert entries["/data/old_1.jsonl"]["stale"] is True


@pytest.mark.asyncio
async def test_list_empty_new_workflow_scope_returns_empty_entries(client):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)

    r = await client.get(f"/api/v1/vfs?wf_id={wf_id}", headers=_hdr(tok))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entries"] == []
    assert body["root_capabilities"] == {}


@pytest.mark.asyncio
async def test_retired_debug_directory_stays_hidden_even_when_debug_is_enabled(
    client, app_engine, pg_engine, monkeypatch
):
    from vibecanvas_api.routes import vfs as vfs_routes

    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    tenant = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(
        app_engine,
        tenant,
        wf_id,
        path="/logs/.debug/snapshot.json",
        content="{}",
        content_type="application/json",
    )
    await _seed_vfs(
        app_engine,
        tenant,
        wf_id,
        path="/logs/.private/secret.json",
        content="{}",
        content_type="application/json",
    )
    monkeypatch.setattr(vfs_routes.config, "agent_debug_view_enabled", True)

    ordinary = await client.get(
        f"/api/v1/vfs?wf_id={wf_id}&prefix=/logs/.debug/",
        headers=_hdr(tok),
    )
    assert ordinary.status_code == 200
    assert ordinary.json()["entries"] == []

    debug = await client.get(
        f"/api/v1/vfs?wf_id={wf_id}&prefix=/logs/.debug/&include_hidden=true",
        headers=_hdr(tok),
    )
    assert debug.json()["entries"] == []

    broad = await client.get(
        f"/api/v1/vfs?wf_id={wf_id}&prefix=/logs/&include_hidden=true",
        headers=_hdr(tok),
    )
    assert broad.json()["entries"] == []


@pytest.mark.asyncio
async def test_content_returns_bounded_body(client, app_engine, pg_engine, monkeypatch):
    from vibecanvas_api.routes import vfs as vfs_routes
    monkeypatch.setattr(vfs_routes, "VFS_HTTP_MAX_BYTES", 128)
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    big = "x" * 300
    await _seed_vfs(app_engine, t, wf_id, path="/files/doc_1.txt",
                    content=big, content_type="text/plain")
    r = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/files/doc_1.txt",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["truncated"] is True
    assert body["size_bytes"] == 300
    assert len(body["content"].encode()) <= 128


@pytest.mark.asyncio
async def test_content_404_for_unknown_path(client, app_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    r = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/data/nope_9.jsonl",
                         headers=_hdr(tok))
    assert r.status_code == 404
    assert r.json()["detail"] == "vfs_path_not_found"


@pytest.mark.asyncio
async def test_content_read_is_no_touch(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/data/keep_1.jsonl", content='{"a":1}')

    async def _la():
        async with app_engine.connect() as c:
            await c.execute(text("SELECT set_config('app.tenant_id',:t,false)"), {"t": t})
            return (await c.execute(text(
                "SELECT last_access FROM vfs_artifacts WHERE scope_id=:w AND path='/data/keep_1.jsonl'"),
                {"w": wf_id})).one()[0]

    before = await _la()
    await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/data/keep_1.jsonl",
                     headers=_hdr(tok))
    after = await _la()
    assert after == before


@pytest.mark.asyncio
async def test_content_route_binary_artifact_returns_descriptor(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    # binary artifact row: content="" (bytes live in the object store),
    # non-text content_type, a real size_bytes.
    await _seed_vfs(app_engine, t, wf_id, path="/data/img_1.png", content="",
                    content_type="image/png", wf_version="v1.sv0", size_bytes=1234)
    r = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/data/img_1.png",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] is None           # descriptor — was "" before the fix
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == 1234


@pytest.mark.asyncio
async def test_content_route_octet_stream_jsonl_uses_extension_fallback(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs_object_text(
        app_engine, t, wf_id,
        path="/data/rows.jsonl",
        content='{"a":1}\n{"a":2}\n',
        content_type="application/octet-stream",
    )

    r = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/data/rows.jsonl",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content_type"] == "table/jsonl"
    assert body["content"] == '{"a":1}\n{"a":2}\n'


@pytest.mark.asyncio
async def test_write_bytes_route_persists_xlsx_as_binary_descriptor(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id, _ = await _workspace_scopes(client, tok, "chat_xlsx")
    payload = b"PK\x03\x04fake-xlsx"

    r = await client.put("/api/v1/vfs/bytes", headers=_hdr(tok), json={
        "wf_id": wf_id,
        "path": "/data/book.xlsx",
        "data_b64": base64.b64encode(payload).decode("ascii"),
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    })
    assert r.status_code == 200, r.text
    assert r.json()["size_bytes"] == len(payload)

    r = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/data/book.xlsx",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] is None
    assert body["content_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.asyncio
async def test_content_route_text_artifact_still_returns_content(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/files/note_1.txt",
                    content="hello world", content_type="text/plain")
    r = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/files/note_1.txt",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "hello world"   # text unchanged
    assert body["content_type"] == "text/plain"


@pytest.mark.asyncio
async def test_content_route_legacy_json_spelling_still_text(client, app_engine, pg_engine):
    """Legacy 2b-1 content_type spelling 'json' must NOT misroute to the binary
    descriptor branch (content would be None) — it is text."""
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/data/legacy_1.json",
                    content='{"a":1}', content_type="json")
    r = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/data/legacy_1.json",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    assert r.json()["content"] == '{"a":1}'


# ---- writable VFS upload route ---------------------------------------------

def _csv_file(name="sales.csv", content=b"a,b\n1,2", ct="text/csv"):
    return {"file": (name, content, ct)}


@pytest.mark.asyncio
async def test_mount_upload_creates_then_replaces(client, app_engine, pg_engine):
    tok = await _register(client)
    _, mount_scope = await _workspace_scopes(client, tok, "chat_mount")
    r = await client.post(f"/api/v1/vfs/upload?wf_id={mount_scope}&folder=mount",
                          files=_csv_file(), headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "/mount/sales.csv"
    assert body["replaced"] is False
    assert body["content_type"] == "text/csv"
    assert body["size_bytes"] == len(b"a,b\n1,2")
    # readable via /content (object-backed text → decoded)
    rc = await client.get(f"/api/v1/vfs/content?wf_id={mount_scope}&path=/mount/sales.csv",
                          headers=_hdr(tok))
    assert rc.status_code == 200, rc.text
    assert rc.json()["content"] == "a,b\n1,2"
    # re-POST same name overwrites → replaced True
    r2 = await client.post(f"/api/v1/vfs/upload?wf_id={mount_scope}&folder=mount",
                           files=_csv_file(content=b"x,y\n9,9"), headers=_hdr(tok))
    assert r2.status_code == 200, r2.text
    assert r2.json()["replaced"] is True
    rc2 = await client.get(f"/api/v1/vfs/content?wf_id={mount_scope}&path=/mount/sales.csv",
                           headers=_hdr(tok))
    assert rc2.json()["content"] == "x,y\n9,9"   # overwrite-in-place


@pytest.mark.asyncio
async def test_mount_upload_traversal_filename_sanitized(client, app_engine, pg_engine):
    tok = await _register(client)
    _, mount_scope = await _workspace_scopes(client, tok, "chat_traversal")
    # os.path.basename strips dir components, so "../../etc/passwd" → "passwd";
    # the file lands at /mount/passwd and never escapes the writable root.
    r = await client.post(f"/api/v1/vfs/upload?wf_id={mount_scope}&folder=mount",
                          files=_csv_file(name="../../etc/passwd"), headers=_hdr(tok))
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "/mount/passwd"


@pytest.mark.asyncio
async def test_mount_upload_dotdot_basename_400(client, app_engine):
    tok = await _register(client)
    _, mount_scope = await _workspace_scopes(client, tok, "chat_dotdot")
    # basename("..") == ".." → _validate_artifact_path rejects the `..` segment.
    r = await client.post(f"/api/v1/vfs/upload?wf_id={mount_scope}&folder=mount",
                          files=_csv_file(name=".."), headers=_hdr(tok))
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_mount_upload_blank_filename_400(client, app_engine):
    tok = await _register(client)
    _, mount_scope = await _workspace_scopes(client, tok, "chat_blank")
    # a whitespace-only filename strips to empty → _validate_artifact_path 400.
    r = await client.post(f"/api/v1/vfs/upload?wf_id={mount_scope}&folder=mount",
                          files={"file": ("   ", b"x", "text/plain")}, headers=_hdr(tok))
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_upload_rejects_workflow_scope(client, app_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    r = await client.post(f"/api/v1/vfs/upload?wf_id={wf_id}&folder=mount",
                          files=_csv_file(), headers=_hdr(tok))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "workflow_vfs_not_writable"


@pytest.mark.asyncio
async def test_mount_upload_oversize_413(client, app_engine, monkeypatch):
    from vibecanvas_api.config import config as app_config
    monkeypatch.setattr(app_config.storage, "vfs_upload_max_bytes", 8)
    tok = await _register(client)
    _, mount_scope = await _workspace_scopes(client, tok, "chat_large")
    r = await client.post(f"/api/v1/vfs/upload?wf_id={mount_scope}&folder=mount",
                          files=_csv_file(content=b"way too many bytes"), headers=_hdr(tok))
    assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_mount_upload_cross_tenant_cannot_read(client, app_engine, pg_engine):
    # tenant A uploads; tenant B (a different registered user) cannot read it.
    tok_a = await _register(client)
    _, mount_a = await _workspace_scopes(client, tok_a, "chat_a")
    r = await client.post(f"/api/v1/vfs/upload?wf_id={mount_a}&folder=mount",
                          files=_csv_file(), headers=_hdr(tok_a))
    assert r.status_code == 200, r.text
    tok_b = await _register(client)
    rc = await client.get(f"/api/v1/vfs/content?wf_id={mount_a}&path=/mount/sales.csv",
                          headers=_hdr(tok_b))
    assert rc.status_code == 404   # RLS hides A's row from B


# ---- /data upload: a user-writable durable prefix ---------------------------


@pytest.mark.asyncio
async def test_data_upload_creates_durable_row(client, app_engine, pg_engine):
    # folder=data writes to the DURABLE /data artifact prefix; readable via /content.
    tok = await _register(client)
    wf_id, _ = await _workspace_scopes(client, tok, "chat_data")
    r = await client.post(f"/api/v1/vfs/upload?wf_id={wf_id}&folder=data",
                          files=_csv_file(name="notes.csv"), headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "/data/notes.csv"
    assert body["replaced"] is False
    rc = await client.get(f"/api/v1/vfs/content?wf_id={wf_id}&path=/data/notes.csv",
                          headers=_hdr(tok))
    assert rc.status_code == 200, rc.text
    assert rc.json()["content"] == "a,b\n1,2"
    # re-POST same name overwrites in place → replaced True
    r2 = await client.post(f"/api/v1/vfs/upload?wf_id={wf_id}&folder=data",
                           files=_csv_file(name="notes.csv", content=b"z,z\n0,0"),
                           headers=_hdr(tok))
    assert r2.status_code == 200, r2.text
    assert r2.json()["replaced"] is True


@pytest.mark.asyncio
async def test_project_workspace_data_upload_does_not_require_workflow(client, app_engine):
    tok = await _register(client)
    scope_id, _ = await _workspace_scopes(client, tok, "chat_upload_1")
    assert scope_id.startswith("__projectws_v1_")

    r = await client.post(
        f"/api/v1/vfs/upload?wf_id={scope_id}&folder=data",
        files=_csv_file(name="notes.csv"),
        headers=_hdr(tok),
    )
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "/data/notes.csv"

    rc = await client.get(
        f"/api/v1/vfs/content?wf_id={scope_id}&path=/data/notes.csv",
        headers=_hdr(tok),
    )
    assert rc.status_code == 200, rc.text
    assert rc.json()["content"] == "a,b\n1,2"

    blocked = await client.post(
        f"/api/v1/vfs/upload?wf_id={scope_id}&folder=mount",
        files=_csv_file(name="bad.csv"),
        headers=_hdr(tok),
    )
    assert blocked.status_code == 400, blocked.text


@pytest.mark.asyncio
async def test_upload_requires_explicit_folder(client, app_engine, pg_engine):
    tok = await _register(client)
    _, mount_scope = await _workspace_scopes(client, tok, "chat_explicit")
    r = await client.post(f"/api/v1/vfs/upload?wf_id={mount_scope}",
                          files=_csv_file(name="d.csv"), headers=_hdr(tok))
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_upload_rejects_non_allowlisted_folder(client, app_engine):
    # /memory (and any other non-allowlisted folder) is NOT user-writable → 400.
    tok = await _register(client)
    wf_id, _ = await _workspace_scopes(client, tok, "chat_invalid")
    r = await client.post(f"/api/v1/vfs/upload?wf_id={wf_id}&folder=memory",
                          files=_csv_file(), headers=_hdr(tok))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid_folder"
