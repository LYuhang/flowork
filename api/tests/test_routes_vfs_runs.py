"""RE-4 T3 — GET /api/v1/vfs/runs/{run_id} run-tier list endpoint.

Mirrors test_routes_vfs_run's real-session harness (register -> bearer token,
tenant discovered from the user's workflow). /run rows have NO inline content —
the bytes live in the ObjectStore — so they are seeded via ``VfsRunRepo`` against
the SAME process-singleton InMemory store (``get_object_store()``), under a
``session_scope`` bound to that tenant.

The list response exposes path/content_type/size_bytes ONLY — never object_key
while RLS keeps each tenant's run rows private.
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
    async with session_scope(tenant_id=tenant) as s:
        repo = VfsRunRepo(s, get_object_store(), tenant)
        await repo.write_bytes(run_id=run_id, path=path, data=data,
                               content_type=content_type, wf_id=wf_id)


@pytest.mark.asyncio
async def test_list_returns_run_entries_without_object_key(client, pg_engine):
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    t = await _dev_tenant(pg_engine, wf_id)
    await _seed_run(t, "r1", wf_id=wf_id, path="/run/n1/a.txt", data=b"hello",
                    content_type="text/plain")
    await _seed_run(t, "r1", wf_id=wf_id, path="/run/n2/b.png",
                    data=b"\x89PNG" + b"\x00" * 10,
                    content_type="image/png")

    r = await client.get("/api/v1/vfs/runs/r1", headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    entries = {e["path"]: e for e in body["entries"]}
    assert set(entries) == {"/run/n1/a.txt", "/run/n2/b.png"}
    assert entries["/run/n1/a.txt"]["content_type"] == "text/plain"
    assert entries["/run/n1/a.txt"]["size_bytes"] == 5
    assert entries["/run/n2/b.png"]["content_type"] == "image/png"
    # No object_key (or any agent-VFS field) is leaked.
    for e in body["entries"]:
        assert "object_key" not in e
        assert set(e) == {"path", "content_type", "size_bytes", "capabilities"}
        assert "read" in e["capabilities"]


@pytest.mark.asyncio
async def test_list_is_tenant_isolated(client, pg_engine):
    # Tenant A seeds a run; tenant B (a different real user) sees nothing (RLS).
    tok_a = await _register(client)
    wf_a = await _create_wf(client, tok_a)
    ta = await _dev_tenant(pg_engine, wf_a)
    await _seed_run(ta, "shared_run", wf_id=wf_a,
                    path="/run/n1/secret.txt", data=b"x",
                    content_type="text/plain")

    tok_b = await _register(client)
    await _create_wf(client, tok_b)  # establishes tenant B
    r = await client.get("/api/v1/vfs/runs/shared_run", headers=_hdr(tok_b))
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "resource_not_found"
