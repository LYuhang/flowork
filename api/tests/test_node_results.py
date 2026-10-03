# -*- coding: utf-8 -*-
"""Per-node run results in the VFS (`/run/__exec__/nodes/{node_id}.json`).

Covers the shared writer/reader + payload helpers (Task 1) and the purge guard
(Task 6 — a new full-run start wipes the prior run's `nodes/*.json`).

The write→read round-trip mirrors `test_run_release.py`: seed a real `tenants`
row, patch a FilesystemObjectStore onto the module's `get_object_store`, and let
`write_node_result`/`read_node_result` open their own tenant-bound
`session_scope`.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

import vibecanvas_api.services.node_results as nr_mod
from vibecanvas_api.services.node_results import (
    build_node_payload,
    node_result_path,
    persist_node_debug_result,
    persist_node_frame_payload,
    read_node_result,
    write_node_result,
)
from vibecanvas_api.services.object_store import FilesystemObjectStore


@pytest.fixture(autouse=True)
def live_result_mirror(monkeypatch):
    manager = AsyncMock()
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.manager.get_sandbox_manager", lambda: manager,
    )
    return manager.mirror_vfs_write


# --------------------------------------------------------------------------- #
# Pure helpers — node_result_path / build_node_payload                         #
# --------------------------------------------------------------------------- #
def test_node_result_path_normal_and_reserved():
    assert node_result_path("node_3") == "/run/__exec__/nodes/node_3.json"
    assert node_result_path("__end__") == "/run/__exec__/nodes/__end__.json"


@pytest.mark.parametrize("bad", ["", "n1", "node_", "../x", "node_1/x", "evil"])
def test_node_result_path_rejects_bad_id(bad):
    with pytest.raises(ValueError):
        node_result_path(bad)


def test_build_node_payload_stamps_ts_when_absent():
    p = build_node_payload(node_id="node_1", status="completed", output={"y": 1})
    assert p["node_id"] == "node_1" and p["status"] == "completed"
    assert p["output"] == {"y": 1}
    assert isinstance(p["ts"], str) and p["ts"]  # stamped


def test_build_node_payload_keeps_passed_ts():
    p = build_node_payload(node_id="node_1", status="completed", ts="2026-06-15T00:00:00+00:00")
    assert p["ts"] == "2026-06-15T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# persist_node_frame_payload — frame → payload mapping                         #
# --------------------------------------------------------------------------- #
def test_persist_frame_completed_maps_result_string_to_output():
    frame = {
        "node_id": "node_2",
        "status": "completed",
        "result": '{"y": 42}',  # JSON string from to_exec_update
        "inputs": {"x": 1},
        "duration": 0.5,
        "node_name": "code2",
        "node_type": "CodeNode",
    }
    p = persist_node_frame_payload(frame)
    assert p is not None
    assert p["output"] == {"y": 42}  # parsed
    assert p["inputs"] == {"x": 1}
    assert p["status"] == "completed"
    assert p["execution_time"] == 0.5
    assert p["node_name"] == "code2" and p["node_type"] == "CodeNode"


def test_persist_frame_bad_result_keeps_raw_string():
    frame = {"node_id": "node_2", "status": "completed", "result": "not json"}
    p = persist_node_frame_payload(frame)
    assert p is not None and p["output"] == "not json"


def test_persist_frame_error_maps_error():
    frame = {"node_id": "node_3", "status": "error", "error": "boom"}
    p = persist_node_frame_payload(frame)
    assert p is not None and p["status"] == "error" and p["error"] == "boom"


def test_persist_frame_running_returns_none():
    assert persist_node_frame_payload(
        {"node_id": "node_1", "status": "running"}) is None


def test_persist_frame_no_node_id_returns_none():
    assert persist_node_frame_payload({"status": "completed", "result": "{}"}) is None


# --------------------------------------------------------------------------- #
# write_node_result / read_node_result — round-trip against a real DB + FS     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_write_read_roundtrip(app_engine, tmp_path, monkeypatch, live_result_mirror):
    tenant = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                        {"t": tenant})
    store = FilesystemObjectStore(root=str(tmp_path))
    monkeypatch.setattr(nr_mod, "get_object_store", lambda: store)

    payload = build_node_payload(
        node_id="node_5", node_name="code5", node_type="CodeNode",
        status="completed", inputs={"a": 1}, output={"b": 2}, execution_time=0.1)
    mirrored = []
    async def mirror_after_commit(tenant_id, run_id, path, data):
        # A separate transaction must see the result before it becomes visible
        # in the sandbox, so release/rebuild cannot lose a merely mirrored file.
        assert await read_node_result(run_id, tenant_id, "node_5") == payload
        assert path == node_result_path("node_5")
        import json
        assert json.loads(data) == payload
        mirrored.append(path)
        return True
    live_result_mirror.side_effect = mirror_after_commit
    await write_node_result("r1", str(tenant), payload)
    live_result_mirror.assert_awaited_once()
    assert mirrored == [node_result_path("node_5")]

    got = await read_node_result("r1", str(tenant), "node_5")
    assert got is not None
    assert got["node_id"] == "node_5"
    assert got["output"] == {"b": 2}
    assert got["inputs"] == {"a": 1}
    assert got["status"] == "completed"


@pytest.mark.asyncio
async def test_read_missing_returns_none(app_engine, tmp_path, monkeypatch):
    tenant = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                        {"t": tenant})
    store = FilesystemObjectStore(root=str(tmp_path))
    monkeypatch.setattr(nr_mod, "get_object_store", lambda: store)

    assert await read_node_result("r1", str(tenant), "node_99") is None


@pytest.mark.asyncio
async def test_write_is_fail_soft(app_engine, tmp_path, monkeypatch, live_result_mirror):
    """A bad node_id (→ ValueError in node_result_path) must NOT raise — the
    fail-soft write swallows it so a results-write never breaks a run."""
    tenant = uuid.uuid4()
    store = FilesystemObjectStore(root=str(tmp_path))
    monkeypatch.setattr(nr_mod, "get_object_store", lambda: store)
    # No exception escapes even though the node_id is invalid.
    await write_node_result("r1", str(tenant), {"node_id": "bogus", "status": "completed"})
    live_result_mirror.assert_not_awaited()


@pytest.mark.asyncio
async def test_mirror_failure_keeps_committed_result(app_engine, tmp_path, monkeypatch, live_result_mirror):
    tenant = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"), {"t": tenant})
    monkeypatch.setattr(nr_mod, "get_object_store", lambda: FilesystemObjectStore(root=str(tmp_path)))
    live_result_mirror.side_effect = RuntimeError("sandbox unavailable")
    payload = build_node_payload(node_id="node_1", status="completed", output={"n": 4})
    await write_node_result("workflow", str(tenant), payload)
    live_result_mirror.assert_awaited_once()
    assert await read_node_result("workflow", str(tenant), "node_1") == payload


# --------------------------------------------------------------------------- #
# Task 3 — single-node debug writes into the workflow's fixed run-tier         #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_node_debug_writes_into_workflow_run(app_engine, tmp_path, monkeypatch):
    """A node-debug terminal frame writes nodes/{node}.json into run_id == wf_id
    and does NOT purge other nodes' files."""
    tenant = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                        {"t": tenant})
    store = FilesystemObjectStore(root=str(tmp_path))
    monkeypatch.setattr(nr_mod, "get_object_store", lambda: store)

    # A pre-existing OTHER node file in the same workflow run — must survive.
    await write_node_result(
        "wf_x", str(tenant),
        build_node_payload(node_id="node_1", status="completed", output={"a": 1}))

    frame = {"node_id": "node_2", "status": "completed", "result": '{"y": 9}'}
    run_id = await persist_node_debug_result("wf_x", str(tenant), frame)
    assert run_id == "wf_x"

    # node_2 written into the workflow run...
    got = await read_node_result("wf_x", str(tenant), "node_2")
    assert got is not None and got["output"] == {"y": 9}
    # ...and node_1 untouched (no purge).
    other = await read_node_result("wf_x", str(tenant), "node_1")
    assert other is not None and other["output"] == {"a": 1}


@pytest.mark.asyncio
async def test_node_debug_without_prior_run_writes_workflow_run(app_engine, tmp_path, monkeypatch):
    """No prior whole-workflow run is required; run_id == wf_id is stable."""
    tenant = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                        {"t": tenant})
    store = FilesystemObjectStore(root=str(tmp_path))
    monkeypatch.setattr(nr_mod, "get_object_store", lambda: store)

    frame = {"node_id": "node_2", "status": "completed", "result": "{}"}
    assert await persist_node_debug_result("wf_x", str(tenant), frame) == "wf_x"
    assert await read_node_result("wf_x", str(tenant), "node_2") is not None


@pytest.mark.asyncio
async def test_node_debug_non_terminal_frame_returns_none(app_engine, tmp_path, monkeypatch):
    """A running frame produces no payload and returns None."""
    frame = {"node_id": "node_2", "status": "running"}
    assert await persist_node_debug_result("wf_x", "t", frame) is None


# --------------------------------------------------------------------------- #
# Task 6 — purge guard: a new full-run start wipes the prior run's nodes/*     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_purge_prior_run_wipes_node_files(vfs_run_repo, monkeypatch):
    """Seed a PRIOR run's `nodes/node_1.json` (via write_node_result), then a
    new run's start (`VfsRunRepo.purge_workflow_runs`) must delete that prior
    run's node-file row while keeping the current run's."""
    # write_node_result opens its own session_scope + get_object_store; point
    # both at THIS repo's session + store so the write lands in the same DB the
    # fixture's repo reads (the fixture already bound the tenant GUC).
    store = vfs_run_repo._os
    monkeypatch.setattr(nr_mod, "get_object_store", lambda: store)

    # Seed the PRIOR run's node file directly through the fixture repo (same
    # session) so wf_id is stamped (purge scopes by wf_id).
    payload = build_node_payload(node_id="node_1", status="completed", output={"y": 1})
    import json as _json
    await vfs_run_repo.write_bytes(
        run_id="old_run",
        path=node_result_path("node_1"),
        data=_json.dumps(payload).encode(),
        content_type="application/json",
        wf_id="wf_1")
    # Current run's node file.
    await vfs_run_repo.write_bytes(
        run_id="new_run",
        path=node_result_path("node_2"),
        data=b"{}",
        content_type="application/json",
        wf_id="wf_1")

    # Prior present before purge.
    assert await vfs_run_repo.read(
        run_id="old_run", path=node_result_path("node_1")) is not None

    purged = await vfs_run_repo.purge_workflow_runs(wf_id="wf_1", except_run_id="new_run")
    assert purged == 1  # old_run

    # The prior run's node-file row is GONE; the current run's survives.
    assert await vfs_run_repo.read(
        run_id="old_run", path=node_result_path("node_1")) is None
    assert await vfs_run_repo.read(
        run_id="new_run", path=node_result_path("node_2")) is not None
