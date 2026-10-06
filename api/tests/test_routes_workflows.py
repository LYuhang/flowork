"""Workflows router smoke: CRUD + commit + edits + check.

Auth: the legacy ``VIBECANVAS_API_DEV_TOKEN`` + sync ``TestClient`` +
``Bearer tok`` harness is DEAD (dev-token auth was removed from the app).
These route-contract tests now use the conftest async ``client`` fixture +
a real ``register → session_token`` (the same pattern as
``test_routes_vfs.py`` / ``test_routes_executions.py``).
"""

from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    """Register a fresh user, return its bearer session token. Email is
    unique per call (uuid) so it never collides with another test's row."""
    email = f"wf_{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_create_then_list(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/workflows", json={"name": "test_wf"},
                          headers=_hdr(tok))
    assert r.status_code == 201, r.text
    wf_id = r.json()["wf_id"]

    r = await client.get("/api/v1/workflows", headers=_hdr(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert any(item["wf_id"] == wf_id for item in body["items"])


@pytest.mark.asyncio
async def test_get_404_for_unknown(client, pg_engine):
    tok = await _register(client)
    r = await client.get("/api/v1/workflows/no_such_wf", headers=_hdr(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_commit_then_check(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/workflows", json={"name": "wf2"},
                          headers=_hdr(tok))
    wf_id = r.json()["wf_id"]

    minimal_wf = {
        "__meta__": {"workflow_id": wf_id, "workflow_version": 1,
                      "workflow_subversion": 0},
        "node_1": {
            "node_id": "node_1", "node_name": "__start__",
            "node_type": "StartNode", "node_description": "",
            "input_fields": {"m": {"type": "string", "value": "", "reference": ""}},
            "output_fields": {"m": {"type": "string", "description": ""}},
            # StartNode.CONFIG_SCHEMA requires node_config == {} (additionalProperties
            # False); the legacy fixture's stale "process_fn" key now fails Check.
            "node_config": {}, "children": ["node_2"],
            "__attributes__": {"x": 0, "y": 0},
        },
        "node_2": {
            "node_id": "node_2", "node_name": "__end__", "node_type": "EndNode",
            "node_description": "",
            "input_fields": {"m": {"type": "string", "value": "", "reference": "__start__.m"}},
            "output_fields": {"m": {"type": "string", "description": ""}},
            "node_config": {}, "children": [],
            "__attributes__": {"x": 200, "y": 0},
        },
    }
    r = await client.post(f"/api/v1/workflows/{wf_id}/commits",
                          json={"workflow": minimal_wf, "expected_version": "v1.sv0"}, headers=_hdr(tok))
    assert r.status_code == 200

    r = await client.post(f"/api/v1/workflows/{wf_id}/check", headers=_hdr(tok))
    assert r.status_code == 200
    assert r.json()["status"] == "success"


@pytest.mark.asyncio
async def test_edits_apply_vibe_op(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/workflows", json={"name": "wf3"},
                          headers=_hdr(tok))
    wf_id = r.json()["wf_id"]

    base_wf = {
        "__meta__": {"workflow_id": wf_id, "workflow_version": 1,
                      "workflow_subversion": 0},
        "node_1": {
            "node_id": "node_1", "node_name": "n1", "node_type": "StartNode",
            "node_description": "", "input_fields": {}, "output_fields": {},
            "node_config": {"process_fn": ""}, "children": [],
            "__attributes__": {"x": 0, "y": 0},
        },
    }
    await client.post(f"/api/v1/workflows/{wf_id}/commits",
                      json={"workflow": base_wf, "expected_version": "v1.sv0"}, headers=_hdr(tok))

    # vibe-op v2 shape: JSON-Patch op + JSON Pointer path.
    edits = [
        ["replace", "/node_1/node_description", "updated"],
    ]
    r = await client.post(f"/api/v1/workflows/{wf_id}/edits",
                          json={"updates": edits}, headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied_count"] == 1
    assert body["total_count"] == 1
    assert body["first_error"] is None


@pytest.mark.asyncio
async def test_delete_returns_204(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/workflows", json={"name": "wf_del"},
                          headers=_hdr(tok))
    wf_id = r.json()["wf_id"]
    r = await client.delete(f"/api/v1/workflows/{wf_id}", headers=_hdr(tok))
    assert r.status_code == 204
