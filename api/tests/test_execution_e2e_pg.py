"""Workflow-page execution persistence regression.

This is the coverage gap that let the G8 ⑤ bug through. The existing
``test_routes_executions.py`` is smoke-only (404s / empty list) — it
never drove a *successful* execution, so it never exercised the
producer's persistence path.

The workflow-page execution producer persists lightweight current-run state via
the async ``ExecutionRepo`` under short tenant-bound ``session_scope()`` writes.

The workflow used is a single ``StartNode`` — the engine's minimally
runnable graph (``Workflow.check`` passes with exactly one reachable
``StartNode``, and ``trigger`` succeeds with no LLM needed).

Auth: the legacy ``VIBECANVAS_API_DEV_TOKEN`` + sync ``TestClient`` +
``Bearer tok`` harness is DEAD. This now uses the conftest async
``client`` + a real ``register → session_token`` (the
``test_routes_vfs.py`` pattern), with SSE consumed via ``client.stream``.

The execution runs through the unified gVisor sandbox path. The fixture selects
a filesystem object store because the in-memory store cannot materialize the
per-run ``/run`` directory. ``RUNSC_PATH`` must point at a usable runsc binary.
"""

from __future__ import annotations

import json
import tempfile
import uuid
import base64

import pytest
from sqlalchemy import text


async def _register(client) -> str:
    email = f"g8e2e_{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def _sandbox_oneshot_fs(monkeypatch):
    """Wire the sandbox-only canvas-Run path for the conftest ``client``
    (no app lifespan, inmemory store by default): cold one-shot backend +
    a filesystem object store rooted at a tmpdir so the per-run ``/run``
    dir can be materialized."""
    from vibecanvas_api.config import config as _cfg
    from vibecanvas_api.services.sandbox import _gvisor_runnable

    if not _gvisor_runnable():
        pytest.skip("full rootless gVisor profile is unavailable")
    monkeypatch.setattr(_cfg.object_store, "provider", "filesystem",
                        raising=False)
    monkeypatch.setattr(_cfg.object_store, "fs_root",
                        tempfile.mkdtemp(prefix="vc-os-"), raising=False)
    monkeypatch.setattr(_cfg, "kms_provider", "local", raising=False)
    monkeypatch.setattr(
        _cfg,
        "kms_local_master_key",
        base64.urlsafe_b64encode(b"e" * 32).decode(),
        raising=False,
    )
    monkeypatch.setattr(_cfg, "kms_local_master_key_file", "", raising=False)


async def _parse_sse_stream(resp) -> list[tuple[str, dict]]:
    """Parse an httpx streaming SSE response into (event_name, data) tuples."""
    events: list[tuple[str, dict]] = []
    buf = ""
    async for chunk in resp.aiter_bytes():
        buf += chunk.decode("utf-8")
        while "\n\n" in buf:
            block, _, buf = buf.partition("\n\n")
            ev_name: str | None = None
            data: dict | None = None
            for line in block.split("\n"):
                if line.startswith("event: "):
                    ev_name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    raw = line.removeprefix("data: ")
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {"raw": raw}
            if ev_name is not None:
                events.append((ev_name, data or {}))
    return events


def _minimal_runnable_workflow(wf_id: str) -> dict:
    """A single ``StartNode`` — the engine's minimally runnable graph."""
    return {
        "__meta__": {
            "workflow_id": wf_id,
            "workflow_version": 1,
            "workflow_subversion": 0,
        },
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "StartNode",
            "node_description": "",
            "input_fields": {},
            "output_fields": {},
            "node_config": {"process_fn": ""},
            "children": [],
            "__attributes__": {"x": 0, "y": 0},
        },
    }


@pytest.mark.asyncio
async def test_real_execution_lands_terminal_row_in_pg(
    client, pg_engine, _sandbox_oneshot_fs,
):
    """Drive a real successful execution end-to-end; assert an
    ``executions`` row lands in Postgres with a terminal status."""
    tok = await _register(client)
    hdr = _hdr(tok)

    # 1. Create a workflow + commit a minimally-runnable graph.
    r = await client.post(
        "/api/v1/workflows", json={"name": "g8_exec_e2e"}, headers=hdr,
    )
    assert r.status_code == 201, r.text
    wf_id = r.json()["wf_id"]

    r = await client.post(
        f"/api/v1/workflows/{wf_id}/commits",
        json={"workflow": _minimal_runnable_workflow(wf_id), "expected_version": "v1.sv0"}, headers=hdr,
    )
    assert r.status_code == 200, r.text

    # 2. POST start-execution; drive the SSE stream to its terminator.
    async with client.stream(
        "POST", f"/api/v1/workflows/{wf_id}/executions",
        json={"mode": "single", "input": {}}, headers=hdr,
    ) as resp:
        assert resp.status_code == 200
        events = await _parse_sse_stream(resp)

    names = [n for n, _ in events]
    assert names, "no SSE events received"
    assert names[0] == "started", f"first event not 'started': {names[:3]}"
    assert names[-1] in {"done", "error"}, (
        f"last event not done/error: {names[-2:]}"
    )
    # Pre-fix regression guard: the bug surfaced as an asyncio.run-in-loop
    # RuntimeError bubbling up as an engine_error frame. A real successful
    # execution must NOT terminate with that.
    for ev_name, payload in events:
        if ev_name == "error":
            msg = str(payload.get("message", ""))
            assert "asyncio.run()" not in msg, (
                f"G8 ⑤ regression: execution crashed with the "
                f"asyncio.run-in-running-loop bug: {payload}"
            )

    # 3. DB-assert: workflow_run_state has a terminal status and a non-null
    #    ended_at. The workflow id is the stable current-run key.
    async with pg_engine.connect() as cx:
        row = (await cx.execute(text(
            "SELECT wf_id, turn_id, status, ended_at, private_ciphertext, "
            "private_nonce, private_key_id FROM workflow_run_state "
            "WHERE wf_id = :wf"), {"wf": wf_id})).mappings().first()

    assert row is not None, (
        f"no workflow_run_state row landed for wf_id={wf_id}. SSE: {events}"
    )
    assert row["status"] in {"success", "error", "stopped"}, (
        f"execution did not reach a terminal status: {dict(row)}"
    )
    assert row["status"] == "success", (
        f"a clean StartNode workflow should succeed: {dict(row)}"
    )
    assert row["ended_at"] is not None, (
        f"terminal workflow_run_state row must have ended_at set: {dict(row)}"
    )
    assert row["private_ciphertext"] and row["private_nonce"]
    assert row["private_key_id"] is not None
