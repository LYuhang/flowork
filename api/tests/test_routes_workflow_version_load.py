"""Regression for the left-Explorer "load a pinned version" flow.

Clicking a version in the Explorer navigates the canvas to
``/workflow/:wfId/version/:vKey`` (vKey == ``v{major}.sv{sub}``), which the
frontend resolves by calling ``GET /api/v1/workflows/{wf_id}/at/v{v}.sv{sv}``.

These tests drive the REAL ASGI app (httpx + ASGITransport, real auth via
/auth/register) end-to-end so a failure in route matching, the ``.sv`` path
literal, int coercion, or the response shape surfaces here rather than only in
the live browser as "Failed to load workflow."
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    email = f"u{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post("/api/v1/auth/register",
                          json={"email": email, "username": "Test User", "password": "pw12345678"})
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _minimal_wf(wf_id: str, desc: str) -> dict:
    return {
        "__meta__": {"workflow_id": wf_id, "workflow_version": 1,
                     "workflow_subversion": 0},
        "node_1": {
            "node_id": "node_1", "node_name": "__start__",
            "node_type": "StartNode", "node_description": desc,
            "input_fields": {},
            "output_fields": {"m": {"type": "string", "description": ""}},
            "node_config": {"process_fn": ""}, "children": [],
            "__attributes__": {"x": 0, "y": 0},
        },
    }


async def _create_wf(client, token: str) -> str:
    r = await client.post("/api/v1/workflows", json={"name": "w"},
                          headers=_hdr(token))
    assert r.status_code == 201, r.text
    return r.json()["wf_id"]


@pytest.mark.asyncio
async def test_version_load_endpoint_returns_pinned_snapshot(client):
    """The exact call the canvas makes for a clicked version must 200 and
    return the {workflow, meta} snapshot for that pin."""
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    # create_workflow seeds an EMPTY init version at v1.sv0 (workflow == {}).
    # Two commits then add v1.sv1 ("first") and v1.sv2 ("second").
    r = await client.post(f"/api/v1/workflows/{wf_id}/commits",
                          json={"workflow": _minimal_wf(wf_id, "first"), "expected_version": "v1.sv0"},
                          headers=_hdr(tok))
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/v1/workflows/{wf_id}/commits",
                          json={"workflow": _minimal_wf(wf_id, "second"), "expected_version": "v1.sv1"},
                          headers=_hdr(tok))
    assert r.status_code == 200, r.text

    # The Explorer lists versions; assert the (major, sub) pairs it would
    # render as vKeys are present — including the empty init pin.
    r = await client.get(f"/api/v1/workflows/{wf_id}/versions",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    versions = r.json()["versions"]
    pairs = {(e["major"], e["sub"]) for e in versions}
    assert {(1, 0), (1, 1), (1, 2)} <= pairs

    # REGRESSION: clicking the EMPTY seeded init pin (v1.sv0, workflow == {})
    # must NOT 404 — the old `if not wf` collapsed empty-but-present into
    # "not found", which the canvas rendered as "Failed to load workflow."
    r = await client.get(f"/api/v1/workflows/{wf_id}/at/v1.sv0",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "workflow" in body and "meta" in body
    assert body["workflow"] == {}

    # And the populated pins load their pinned snapshot bytes.
    r = await client.get(f"/api/v1/workflows/{wf_id}/at/v1.sv1",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    assert r.json()["workflow"]["node_1"]["node_description"] == "first"

    r = await client.get(f"/api/v1/workflows/{wf_id}/at/v1.sv2",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    assert r.json()["workflow"]["node_1"]["node_description"] == "second"


@pytest.mark.asyncio
async def test_version_load_unknown_pin_is_404_not_500(client):
    """A pin that does not exist must 404 (handled), never 500 / route miss."""
    tok = await _register(client)
    wf_id = await _create_wf(client, tok)
    r = await client.get(f"/api/v1/workflows/{wf_id}/at/v9.sv9",
                         headers=_hdr(tok))
    assert r.status_code == 404, r.text
