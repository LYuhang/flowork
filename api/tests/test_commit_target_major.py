"""UX-5: editable historical versions — ``POST /commits`` with ``target_major``.

These run through the LIVE auth harness (register → session_token → Bearer),
the only working route harness on this branch (the older
``VIBECANVAS_API_DEV_TOKEN`` / Bearer-"tok" tests are dead — see
``test_routes_workflows.py``). Driving the full stack also seeds a real
``users.user_id`` UUID, which the raw-repo ``test_workflow_repo_pg.py`` style
can no longer do (it predates the UUID FK migration).

Asserted contract:
  - default commit (no ``target_major``) appends to the ACTIVE major;
  - ``target_major=m`` appends a new sub UNDER major ``m`` and MOVES HEAD onto
    it (git checkout-then-commit semantics), even when a newer major is active;
  - a nonexistent ``target_major`` 404s.
"""

from __future__ import annotations

import uuid

import pytest


async def _register(client, label: str) -> dict:
    # Unique email per call (uuid) so registrations never collide, even when
    # several users are created within / across tests.
    email = f"{label}_{uuid.uuid4().hex[:12]}@example.com"
    tok = (await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )).json()["session_token"]
    return {"Authorization": f"Bearer {tok}"}


async def _versions(client, wf_id: str, hdr: dict) -> list[dict]:
    r = await client.get(f"/api/v1/workflows/{wf_id}/versions", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["versions"]


def _subs(versions: list[dict], major: int) -> list[int]:
    return sorted(v["sub"] for v in versions if v["major"] == major)


@pytest.mark.asyncio
async def test_commit_target_major_lands_under_historical_and_moves_head(client):
    hdr = await _register(client, "ux5a")
    wf_id = (await client.post(
        "/api/v1/workflows", json={"name": "H"}, headers=hdr,
    )).json()["wf_id"]

    # v1.sv1 (create() seeded v1.sv0).
    r = await client.post(f"/api/v1/workflows/{wf_id}/commits",
                          json={"workflow": {"node_1": {"node_type": "StartNode"}}, "expected_version": "v1.sv0"},
                          headers=hdr)
    assert r.status_code == 200, r.text
    assert (r.json()["active_v"], r.json()["active_sv"]) == (1, 1)

    # Branch a new major → v2.sv0; HEAD now at (2, 0).
    r = await client.post(f"/api/v1/workflows/{wf_id}/major-versions",
                          json={"workflow": {"node_2": {}}}, headers=hdr)
    assert r.status_code == 200, r.text
    assert (r.json()["active_v"], r.json()["active_sv"]) == (2, 0)

    # Edit the HISTORICAL v1 line and Save under it.
    r = await client.post(
        f"/api/v1/workflows/{wf_id}/commits",
        json={"workflow": {"node_1": {"node_type": "StartNode"}, "edited": True},
              "target_major": 1, "expected_version": "v1.sv1"},
        headers=hdr)
    assert r.status_code == 200, r.text
    # HEAD followed the just-saved historical line → v1.sv2.
    assert (r.json()["active_v"], r.json()["active_sv"]) == (1, 2)

    versions = await _versions(client, wf_id, hdr)
    assert _subs(versions, 1) == [0, 1, 2]
    assert _subs(versions, 2) == [0]

    # The active workflow content reflects the historical edit.
    snap = (await client.get(f"/api/v1/workflows/{wf_id}", headers=hdr)).json()
    assert snap["workflow"].get("edited") is True

    # A plain commit (no target) now appends to the ACTIVE major (= 1).
    r = await client.post(f"/api/v1/workflows/{wf_id}/commits",
                          json={"workflow": {"node_1": {}}, "expected_version": "v1.sv2"}, headers=hdr)
    assert r.status_code == 200, r.text
    assert (r.json()["active_v"], r.json()["active_sv"]) == (1, 3)


@pytest.mark.asyncio
async def test_commit_default_target_unchanged_appends_to_active(client):
    """Regression guard: omitting target_major keeps the legacy behaviour —
    commit to the active major, advance its sub."""
    hdr = await _register(client, "ux5b")
    wf_id = (await client.post(
        "/api/v1/workflows", json={"name": "D"}, headers=hdr,
    )).json()["wf_id"]
    for expected_sv in (1, 2, 3):
        r = await client.post(f"/api/v1/workflows/{wf_id}/commits",
                              json={"workflow": {"k": expected_sv}, "expected_version": f"v1.sv{expected_sv - 1}"}, headers=hdr)
        assert r.status_code == 200, r.text
        assert (r.json()["active_v"], r.json()["active_sv"]) == (1, expected_sv)


@pytest.mark.asyncio
async def test_commit_target_major_unknown_404s(client):
    hdr = await _register(client, "ux5c")
    wf_id = (await client.post(
        "/api/v1/workflows", json={"name": "U"}, headers=hdr,
    )).json()["wf_id"]
    r = await client.post(f"/api/v1/workflows/{wf_id}/commits",
                          json={"workflow": {"x": 1}, "target_major": 99, "expected_version": "v99.sv0"},
                          headers=hdr)
    assert r.status_code == 404, r.text
