"""VFS signed-URL raw-bytes media endpoint (UX-10e).

POST /api/v1/vfs/sign  — authed; mints a signed /raw URL (sig,exp,tenant).
GET  /api/v1/vfs/raw   — NO Bearer; authorized by the signature alone.

Auth uses the same real-session harness as test_routes_vfs.py (register →
session_token → Bearer). VFS rows are seeded directly into the user's tenant
(discovered from the workflow they create) so the RLS-bound read can see them.
"""
from __future__ import annotations

import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text

from vibecanvas_api.services.vfs_signing import sign_vfs_url


async def _register(client) -> str:
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


async def _dev_tenant(pg_engine, wf_id: str) -> str:
    async with pg_engine.connect() as c:
        row = (await c.execute(
            text("SELECT tenant_id FROM workflows WHERE wf_id=:w"), {"w": wf_id})).one()
    return str(row[0])


async def _seed_vfs(app_engine, tenant: str, wf_id: str, *, path, data: bytes,
                    content_type, size_bytes=None):
    from vibecanvas_api.services.object_store import get_object_store
    key = f"artifacts/{tenant}/{wf_id}{path}"
    get_object_store().put_bytes(key, data, content_type)
    async with app_engine.connect() as c:
        await c.execute(text("SELECT set_config('app.tenant_id',:t,false)"), {"t": tenant})
        await c.execute(text(
            "INSERT INTO vfs_artifacts(scope_id,path,object_key,content_type,size_bytes) "
            "VALUES (:w,:p,:k,:ct,:s)"),
            {"w": wf_id, "p": path, "k": key, "ct": content_type,
             "s": size_bytes if size_bytes is not None else len(data)})
        await c.commit()


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # plausible image bytes


# ---- POST /vfs/sign ---------------------------------------------------------

@pytest.mark.asyncio
async def test_sign_requires_auth(client, app_engine):
    r = await client.post("/api/v1/vfs/sign", json={"path": "/data/img_1.png"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_sign_returns_url_with_sig_exp_tenant(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    r = await client.post("/api/v1/vfs/sign",
                          json={"path": "/data/img_1.png", "wf_id": wf_id},
                          headers=_hdr(tok))
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith("/api/v1/vfs/raw?")
    q = parse_qs(urlparse(url).query)
    assert q["sig"][0]
    assert int(q["exp"][0]) > int(time.time())
    assert q["tenant"][0] == t          # tenant from auth ctx, not the client
    assert q["path"][0] == "/data/img_1.png"


# ---- GET /vfs/raw -----------------------------------------------------------

@pytest.mark.asyncio
async def test_raw_valid_sig_returns_bytes_no_auth(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/data/pic_1.png",
                    data=_PNG, content_type="image/png")
    sign = await client.post("/api/v1/vfs/sign",
                             json={"path": "/data/pic_1.png", "wf_id": wf_id},
                             headers=_hdr(tok))
    url = sign.json()["url"]
    # NO Authorization header on the raw fetch.
    r = await client.get(url)
    assert r.status_code == 200, r.text
    assert r.content == _PNG
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "inline" in r.headers["content-disposition"]


@pytest.mark.asyncio
async def test_raw_legacy_workflow_memory_still_reads_scratch(client, app_engine, pg_engine):
    from vibecanvas_api.services.object_store import get_object_store
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.vfs_store import VfsRepo

    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    tenant = await _dev_tenant(pg_engine, wf_id)
    path = "/memory/legacy-preview.png"
    async with session_scope(tenant_id=tenant) as session:
        await VfsRepo(session, object_store=get_object_store()).write_scratch_bytes(
            wf_id=wf_id, tenant=tenant, path=path, data=_PNG, content_type="image/png",
        )
    signed = await client.post(
        "/api/v1/vfs/sign", json={"path": path, "wf_id": wf_id}, headers=_hdr(tok),
    )
    assert signed.status_code == 200, signed.text
    response = await client.get(signed.json()["url"])
    assert response.status_code == 200, response.text
    assert response.content == _PNG


@pytest.mark.asyncio
async def test_raw_tampered_sig_403(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/data/pic_2.png",
                    data=_PNG, content_type="image/png")
    url = (await client.post("/api/v1/vfs/sign",
                             json={"path": "/data/pic_2.png", "wf_id": wf_id},
                             headers=_hdr(tok))).json()["url"]
    r = await client.get(url + "deadbeef")   # corrupt the sig tail
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_raw_tampered_path_403(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/data/pic_3.png",
                    data=_PNG, content_type="image/png")
    await _seed_vfs(app_engine, t, wf_id, path="/data/secret_3.png",
                    data=b"secret", content_type="image/png")
    url = (await client.post("/api/v1/vfs/sign",
                             json={"path": "/data/pic_3.png", "wf_id": wf_id},
                             headers=_hdr(tok))).json()["url"]
    # swap the path query param — sig no longer matches → 403
    bad = url.replace("path=%2Fdata%2Fpic_3.png", "path=%2Fdata%2Fsecret_3.png")
    assert bad != url
    r = await client.get(bad)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_raw_tampered_tenant_403(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/data/pic_4.png",
                    data=_PNG, content_type="image/png")
    url = (await client.post("/api/v1/vfs/sign",
                             json={"path": "/data/pic_4.png", "wf_id": wf_id},
                             headers=_hdr(tok))).json()["url"]
    bad = url.replace(f"tenant={t}", f"tenant={uuid.uuid4()}")
    assert bad != url
    r = await client.get(bad)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_raw_expired_sig_403(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/data/pic_5.png",
                    data=_PNG, content_type="image/png")
    # mint an already-expired URL via the signing util directly (deterministic)
    url = sign_vfs_url(tenant_id=t, path="/data/pic_5.png", wf_id=wf_id,
                       expires_in_s=-10)
    r = await client.get(url)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_raw_html_forced_to_octet_stream_attachment(client, app_engine, pg_engine):
    """A text/html file must NOT be served inline — forced to octet-stream +
    attachment so the endpoint can't host HTML/JS (XSS guard)."""
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_vfs(app_engine, t, wf_id, path="/data/page_6.html",
                    data=b"<script>alert(1)</script>", content_type="text/html")
    url = (await client.post("/api/v1/vfs/sign",
                             json={"path": "/data/page_6.html", "wf_id": wf_id},
                             headers=_hdr(tok))).json()["url"]
    r = await client.get(url)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_raw_cross_tenant_cannot_read(client, app_engine, pg_engine):
    """A sig minted for tenant A scopes the read to A; even if an attacker knew
    B's file path + wf, they cannot read it because the signed tenant (A) is the
    RLS scope and the sig only validates for A's params."""
    tok_a = await _register(client)
    wf_a = await _create_wf(client, tok_a)
    ta = await _dev_tenant(pg_engine, wf_a)
    tok_b = await _register(client)
    wf_b = await _create_wf(client, tok_b)
    tb = await _dev_tenant(pg_engine, wf_b)
    # B's private file
    await _seed_vfs(app_engine, tb, wf_b, path="/data/bsecret_7.png",
                    data=b"B-only", content_type="image/png")
    # A signs a URL for B's file/wf (A controls path+wf_id) — tenant baked = A.
    url = sign_vfs_url(tenant_id=ta, path="/data/bsecret_7.png", wf_id=wf_b)
    r = await client.get(url)
    # RLS scoped to A sees no /data/bsecret_7.png under wf_b → 404, not B's bytes.
    assert r.status_code == 404
    assert r.content != b"B-only"


@pytest.mark.asyncio
async def test_raw_unknown_path_404(client, app_engine, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    url = sign_vfs_url(tenant_id=t, path="/data/nope_8.png", wf_id=wf_id)
    r = await client.get(url)
    assert r.status_code == 404
