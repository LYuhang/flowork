# -*- coding: utf-8 -*-
"""RE-2 T1 — release run-tier at the run-end sites with the correct call shape.

The in-process host fallback was removed, so sandbox reconciliation
so the deployment ``invoke_sync``, the async deployment ``_run`` and the
executions ``_produce_execution`` no longer release the run-tier inline. The
run-filesystem lifecycle (sync-store-back → release, with retain semantics +
fail-soft) is now owned by :class:`RunWorkspace`
(``test_run_workspace.py``) and the sandboxed runner
(``test_sandbox_run_workflow.py``). The four async in-process-wiring tests that
patched ``drain_astream`` / the in-process ``Workflow.trigger`` /
``VfsRunRepo.release`` were therefore DELETED — their contracts are covered as:

* run-tier release(retain=False) sync CM + fail-soft + sweep order →
  ``test_run_workspace.py::test_sync_cm_facades_order_and_sweep`` /
  ``::test_sync_cm_rmtree_runs_even_if_release_sync_raises``.
* run-tier release(retain=True) (the executions producer's retain) → the async
  CM ``test_run_workspace.py::test_async_aexit_sync_then_release_order`` (retain
  threaded through) + ``_produce_execution_sandbox`` constructs
  ``RunWorkspace(..., retain=True)``.
* async-side fail-soft (a release raise doesn't mask the run) →
  ``test_run_workspace.py::test_async_sync_store_raise_still_releases``.

What REMAINS here (still UNIQUE to the synchronous background shell):
``PostgresVfsRunStore.release_sync`` delete/retain against a real DB + the
the shell's release_sync(retain=False) + its fail-soft. The synchronous shell
``deployment_invoke`` still owns its own ``release_sync(run_id=task_id)`` in a
``finally`` (the ``asyncio.run`` having returned), which RunWorkspace does NOT
cover (it's the SYNC shell wrapping the to_thread sandbox runner).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

import vibecanvas_api.background_tasks.deployment_invoke as background_mod
import vibecanvas_api.storage.vfs_run_repo as vfs_run_repo_mod
from vibecanvas_api.services.object_store import FilesystemObjectStore
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.vfs_run_repo import PostgresVfsRunStore


# --------------------------------------------------------------------------- #
# 1. release_sync deletes / retains against a real DB + FS store              #
# --------------------------------------------------------------------------- #
async def test_release_sync_deletes(app_engine, tmp_path, monkeypatch):
    """release_sync(retain=False) deletes the run's rows → ls_sync empty."""
    tenant = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                        {"t": tenant})

    store = FilesystemObjectStore(root=str(tmp_path))
    monkeypatch.setattr(vfs_run_repo_mod, "get_object_store", lambda: store)

    def _drive():
        token = current_sync_tenant_id.set(str(tenant))
        try:
            facade = PostgresVfsRunStore()
            facade.write_bytes_sync(
                run_id="r1", path="/run/n1/a.txt", data=b"a",
                content_type="text/plain")
            assert facade.ls_sync(run_id="r1")  # present before release
            facade.release_sync(run_id="r1", retain=False)
            return facade.ls_sync(run_id="r1")
        finally:
            current_sync_tenant_id.reset(token)

    rows = await asyncio.to_thread(_drive)
    assert rows == []  # deleted


async def test_release_sync_retains(app_engine, tmp_path, monkeypatch):
    """release_sync(retain=True) keeps the run's rows → ls_sync non-empty."""
    tenant = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                        {"t": tenant})

    store = FilesystemObjectStore(root=str(tmp_path))
    monkeypatch.setattr(vfs_run_repo_mod, "get_object_store", lambda: store)

    def _drive():
        token = current_sync_tenant_id.set(str(tenant))
        try:
            facade = PostgresVfsRunStore()
            facade.write_bytes_sync(
                run_id="r1", path="/run/n1/a.txt", data=b"a",
                content_type="text/plain")
            facade.release_sync(run_id="r1", retain=True)
            return facade.ls_sync(run_id="r1")
        finally:
            current_sync_tenant_id.reset(token)

    rows = await asyncio.to_thread(_drive)
    assert len(rows) == 1  # kept


# --------------------------------------------------------------------------- #
# Sync-shell release harness                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_sync_tenant_cv():
    """The run-end sites set ``current_sync_tenant_id`` (production behavior).
    Reset it after each test so this file never leaks the CV into a sibling
    test that asserts the default (test-isolation hygiene)."""
    yield
    current_sync_tenant_id.set(None)


# --------------------------------------------------------------------------- #
# background _run via the SYNC shell → release_sync(retain=False)             #
#                                                                              #
# This is the ONLY in-process call site that still owns its own release: the   #
# ``deployment_invoke`` releases in its genuinely-SYNC shell                   #
# (after ``asyncio.run(_run(...))`` returns) — RunWorkspace does NOT cover it  #
# (the sandbox runner runs INSIDE the to_thread hop; this shell wraps it).     #
# The deployment ``invoke_sync`` route + the executions ``_produce_execution`` #
# tests that used to live here were DELETED (sandbox-only; see the module      #
# docstring for where each contract is now covered).                          #
# --------------------------------------------------------------------------- #
def test_background_shell_releases(monkeypatch):
    captured = {}
    t = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    async def _fake_run(*, task_id, tenant_id, deployment_id, inputs):
        # the async driver ran fine; release happens in the sync shell after.
        captured["ran"] = True

    def _fake_release_sync(self, *, run_id, retain=False):
        captured["call"] = (run_id, retain)
        # release must run from a genuinely-sync frame (no running loop).
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()

    monkeypatch.setattr(background_mod, "_run", _fake_run)
    monkeypatch.setattr(
        PostgresVfsRunStore, "release_sync", _fake_release_sync, raising=True)

    background_mod.deployment_invoke(**dict(
        task_id=task_id, tenant_id=t,
        deployment_id=str(uuid.uuid4()), inputs={}))

    assert captured.get("ran") is True
    assert captured["call"] == (task_id, False)  # resolved run_id == task id


def test_background_shell_release_is_fail_soft(monkeypatch):
    """A release failure in the background shell does not crash the task."""
    t = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    async def _fake_run(*, task_id, tenant_id, deployment_id, inputs):
        return None

    def _boom(self, *, run_id, retain=False):
        raise RuntimeError("db down")

    monkeypatch.setattr(background_mod, "_run", _fake_run)
    monkeypatch.setattr(
        PostgresVfsRunStore, "release_sync", _boom, raising=True)

    # No exception escapes the task body (fail-soft).
    background_mod.deployment_invoke(**dict(
        task_id=task_id, tenant_id=t,
        deployment_id=str(uuid.uuid4()), inputs={}))
