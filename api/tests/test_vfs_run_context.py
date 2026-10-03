# -*- coding: utf-8 -*-
"""Execution-local ``/run`` lifecycle tests.

Workflow-persistent files live in ``/mount``.  This module therefore tests only
the per-execution run directory and its optional result projection; there is no
workflow storage staging or write-back contract.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest

import vibecanvas_api.services.vfs_run_context as rc_mod
from tests.test_vfs_store import _seed_pg
from vibecanvas_api.services.object_store import FilesystemObjectStore, InMemoryObjectStore
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo


def test_build_run_context_returns_only_run_identity_and_directory(monkeypatch, tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path))
    monkeypatch.setattr(rc_mod, "get_object_store", lambda: store)

    context = rc_mod.build_run_context("r1", "tenant-A")

    assert set(context) == {"run_id", "run_dir"}
    assert context["run_id"] == "r1"
    assert os.path.isdir(context["run_dir"])
    assert context["run_dir"].endswith(os.path.join("run", "tenant-A", "r1"))


@pytest.mark.parametrize(
    ("rel", "data", "expected"),
    [
        ("out.csv", b"a,b\n1,2", "table/csv"),
        ("rows.jsonl", b'{"a":1}\n', "table/jsonl"),
        ("doc.json", b"{}", "application/json"),
        ("note", b"plain text", "text/plain"),
        ("blob", b"\x00\x01", "application/octet-stream"),
        ("image.png", b"\x89PNG", "image/png"),
    ],
)
def test_guess_content_type(rel, data, expected):
    assert rc_mod._guess_ct(rel, data) == expected


def _patch_scope_and_store(monkeypatch, pg_session, store):
    @asynccontextmanager
    async def _scope(**_kwargs):
        yield pg_session

    monkeypatch.setattr(rc_mod, "short_session_scope", _scope)
    monkeypatch.setattr(rc_mod, "get_object_store", lambda: store)


@pytest.mark.asyncio
async def test_sync_run_back_projects_execution_files(monkeypatch, pg_session, tmp_path):
    tenant, _wf_id, _ = await _seed_pg(pg_session)
    store = InMemoryObjectStore()
    run_dir = str(tmp_path / "run")
    os.makedirs(os.path.join(run_dir, "sub"), exist_ok=True)
    (tmp_path / "run" / "result.txt").write_bytes(b"ok")
    (tmp_path / "run" / "sub" / "nested.json").write_bytes(b'{"k":1}')
    _patch_scope_and_store(monkeypatch, pg_session, store)

    assert await rc_mod.sync_run_back("run-1", tenant, run_dir) == 2

    repo = VfsRunRepo(pg_session, store, tenant)
    entries = {entry.path for entry in await repo.ls(run_id="run-1")}
    assert entries == {"/run/result.txt", "/run/sub/nested.json"}


@pytest.mark.asyncio
async def test_writeback_does_not_replace_a_live_cli_result_file(monkeypatch, pg_session, tmp_path):
    tenant, _wf_id, _ = await _seed_pg(pg_session)
    store = FilesystemObjectStore(root=str(tmp_path / "objects"))
    run_id = "live-cli"
    run_dir = store.materialize_prefix(f"run/{tenant}/{run_id}/")
    _patch_scope_and_store(monkeypatch, pg_session, store)
    output_path = os.path.join(run_dir, "result.jsonl")
    # CLI keeps this fd open while workflow workers trigger VFS writeback.
    with open(output_path, "wb") as output:
        inode = os.fstat(output.fileno()).st_ino
        await rc_mod.sync_run_back(run_id, tenant, run_dir)
        assert os.stat(output_path).st_ino == inode
        output.write(b'{"output":{"y":7}}\n')
        output.flush()
        # Another snapshot can be stale by the time the DB write completes.
        monkeypatch.setattr(rc_mod, "_collect_run_files", lambda *_: [("result.jsonl", b"")])
        await rc_mod.sync_run_back(run_id, tenant, run_dir)
        assert os.stat(output_path).st_ino == inode
        with open(output_path, "rb") as reader:
            assert reader.read() == b'{"output":{"y":7}}\n'


@pytest.mark.asyncio
async def test_sync_run_back_noops_without_directory(monkeypatch, pg_session, tmp_path):
    store = InMemoryObjectStore()
    _patch_scope_and_store(monkeypatch, pg_session, store)
    assert await rc_mod.sync_run_back("run-0", "tenant-A", None) == 0
    assert await rc_mod.sync_run_back("run-0", "tenant-A", str(tmp_path / "missing")) == 0


@pytest.mark.asyncio
async def test_clear_run_keeps_live_workspace_files_and_bind_directories(monkeypatch, pg_session, tmp_path):
    tenant, _wf_id, _ = await _seed_pg(pg_session)
    store = FilesystemObjectStore(root=str(tmp_path / "objects"))
    _patch_scope_and_store(monkeypatch, pg_session, store)
    run_dir = store.materialize_prefix(f"run/{tenant}/canvas-run/")
    mounted = {}
    for folder in ("data", "memory", "logs", "chats"):
        path = os.path.join(run_dir, folder)
        os.makedirs(path)
        mounted[path] = os.stat(path).st_ino
        with open(os.path.join(path, "old-output.txt"), "wb") as output:
            output.write(b"previous execution")
    with open(os.path.join(run_dir, "old-node-output.json"), "w") as output:
        output.write("{}")
    await rc_mod.clear_run_contents("canvas-run", tenant)
    assert not os.path.exists(os.path.join(run_dir, "old-node-output.json"))
    for path, inode in mounted.items():
        assert os.path.isdir(path)
        assert os.stat(path).st_ino == inode
        assert os.listdir(path) == ["old-output.txt"]
        assert open(os.path.join(path, "old-output.txt"), "rb").read() == b"previous execution"


def test_sync_run_back_sync_uses_sync_facade(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "sub").mkdir(parents=True)
    (run_dir / "sub" / "result.txt").write_text("ok", encoding="utf-8")
    calls = []

    class _SyncStore:
        def write_many_sync(self, **kwargs):
            calls.append(kwargs)
            return len(kwargs["items"])

    monkeypatch.setattr(rc_mod, "PostgresVfsRunStore", _SyncStore)

    assert rc_mod.sync_run_back_sync(
        "run-sync", "tenant-A", str(run_dir), "workflow-A"
    ) == 1
    assert calls == [{
        "run_id": "run-sync",
        "items": [("/run/sub/result.txt", b"ok", "text/plain")],
        "wf_id": "workflow-A",
        "from_materialized": True,
    }]


@pytest.mark.asyncio
async def test_purge_prior_workflow_runs_keeps_current(monkeypatch, pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    store = InMemoryObjectStore()
    _patch_scope_and_store(monkeypatch, pg_session, store)
    repo = VfsRunRepo(pg_session, store, tenant)
    await repo.write_bytes(
        run_id="old", path="/run/a.txt", data=b"a",
        content_type="text/plain", wf_id=wf_id,
    )
    await repo.write_bytes(
        run_id="new", path="/run/b.txt", data=b"b",
        content_type="text/plain", wf_id=wf_id,
    )

    assert await rc_mod.purge_prior_workflow_runs(wf_id, tenant, "new") == 1
    assert await repo.read(run_id="old", path="/run/a.txt") is None
    assert await repo.read(run_id="new", path="/run/b.txt") is not None


def test_run_collection_excludes_persistent_chat_workspace(tmp_path):
    for folder in ("data", "memory", "logs", "chats"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "private.txt").write_text("private Chat data")
    (tmp_path / "node_1").mkdir()
    (tmp_path / "node_1" / "result.txt").write_text("node result")
    assert rc_mod._collect_run_files(str(tmp_path), "workflow") == [("node_1/result.txt", b"node result")]
