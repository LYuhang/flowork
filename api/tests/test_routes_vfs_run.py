"""RE-1 T6 — read-API binary-capable: the /run tier read over GET /vfs/content.

Mirrors test_routes_vfs's real-session harness (register -> bearer token,
tenant discovered from the user's workflow). /run rows have NO inline content —
the bytes live in the ObjectStore — so they are seeded via ``VfsRunRepo`` against
the SAME process-singleton InMemory store the route reads through
(``get_object_store()``), under a ``session_scope`` bound to that tenant.

A text /run file returns its decoded content; a binary one returns a DESCRIPTOR
(content=None + content_type + size_bytes).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo


from tests.test_workflow_authorization_integration import (
    _browser_sessions, _headers as _session_headers, _register as _browser_register,
)


async def _register(client):
    token, _ = await _browser_register(client, 'vfs')
    return token


def _hdr(token: str) -> dict:
    return _session_headers(token)


async def _create_wf(client, token: str) -> str:
    r = await client.post("/api/v1/workflows", json={"name": "w"}, headers=_hdr(token))
    assert r.status_code == 201, r.text
    return r.json()["wf_id"]


async def _dev_tenant(pg_engine, wf_id: str) -> str:
    async with pg_engine.connect() as c:
        row = (await c.execute(
            text("SELECT tenant_id FROM workflows WHERE wf_id=:w"), {"w": wf_id})).one()
    return str(row[0])


async def _seed_run(
    tenant: str, run_id: str, *, wf_id: str, path, data, content_type
):
    """Seed a /run entry: bytes -> the singleton InMemory store the route reads,
    metadata row -> vfs_run under the tenant's RLS GUC."""
    async with session_scope(tenant_id=tenant) as s:
        repo = VfsRunRepo(s, get_object_store(), tenant)
        await repo.write_bytes(run_id=run_id, path=path, data=data,
                               content_type=content_type, wf_id=wf_id)


@pytest.mark.asyncio
async def test_run_binary_returns_descriptor(client, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    await _seed_run(t, "r1", wf_id=wf_id, path="/run/n1/img.png", data=png,
                    content_type="image/png")

    r = await client.get(
        "/api/v1/vfs/content?run_id=r1&path=/run/n1/img.png", headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] is None
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == len(png)
    assert body["truncated"] is False
    assert body["run_id"] == "r1"


@pytest.mark.asyncio
async def test_run_text_returns_content(client, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_run(t, "r1", wf_id=wf_id, path="/run/n1/note.txt", data=b"hello",
                    content_type="text/plain")

    r = await client.get(
        "/api/v1/vfs/content?run_id=r1&path=/run/n1/note.txt", headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "hello"
    assert body["content_type"] == "text/plain"
    assert body["size_bytes"] == 5
    assert body["run_id"] == "r1"


@pytest.mark.asyncio
async def test_run_404_for_unknown_path(client, pg_engine):
    tok = await _register(client)
    await _create_wf(client, tok)
    r = await client.get(
        "/api/v1/vfs/content?run_id=r1&path=/run/n1/nope.bin", headers=_hdr(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_download_link_rechecks_request_identity(client, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    tenant = await _dev_tenant(pg_engine, wf_id)
    await _seed_run(tenant, wf_id, wf_id=wf_id, path="/run/shared.txt", data=b"evidence", content_type="text/plain")
    signed = await client.post('/api/v1/vfs/sign', headers=_hdr(tok),
        json={'run_id': wf_id, 'path': '/run/shared.txt'})
    assert signed.status_code == 200, signed.text
    url = signed.json()['url']
    assert url.startswith('/api/v1/vfs/run-content?')
    response = await client.get(url, headers=_hdr(tok))
    assert response.status_code == 200, response.text
    assert response.content == b'evidence'
    assert response.headers['cache-control'] == 'private, no-store'
    other = await _register(client)
    denied = await client.get(url, headers=_hdr(other))
    assert denied.status_code == 404
