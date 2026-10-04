"""Execution HTTP contracts using browser Cookie/CSRF authentication."""

from __future__ import annotations

import uuid

import pytest


from tests.test_workflow_authorization_integration import _browser_sessions, _register as _register_user


async def _register(client) -> dict:
    headers, _ = await _register_user(client, 'execution')
    return headers


def _hdr(headers: dict) -> dict:
    return headers


@pytest.mark.asyncio
async def test_execution_status_404_for_unknown(client, pg_engine):
    tok = await _register(client)
    r = await client.get("/api/v1/executions/unknown", headers=_hdr(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cancel_404_for_unknown(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/executions/unknown/cancel", headers=_hdr(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_executions_empty(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/workflows", json={"name": "wf"},
                          headers=_hdr(tok))
    assert r.status_code == 201, r.text
    wf_id = r.json()["wf_id"]
    r = await client.get(f"/api/v1/workflows/{wf_id}/executions",
                         headers=_hdr(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_workflow_execution_status_is_null_before_first_run(client, pg_engine):
    tok = await _register(client)
    created = await client.post(
        "/api/v1/workflows", json={"name": "fresh"}, headers=_hdr(tok),
    )
    assert created.status_code == 201, created.text

    response = await client.get(
        f"/api/v1/workflows/{created.json()['wf_id']}/execution/status",
        headers=_hdr(tok),
    )

    assert response.status_code == 200, response.text
    assert response.json() is None


@pytest.mark.asyncio
async def test_workflow_execution_status_404_for_unknown_workflow(client, pg_engine):
    tok = await _register(client)
    response = await client.get(
        "/api/v1/workflows/no_such_wf/execution/status", headers=_hdr(tok),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_start_execution_404_for_unknown_wf(client, pg_engine):
    tok = await _register(client)
    r = await client.post(
        "/api/v1/workflows/no_such_wf/executions",
        json={"mode": "single", "input": {}}, headers=_hdr(tok),
    )
    assert r.status_code == 404
