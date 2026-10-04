# -*- coding: utf-8 -*-
"""Resident-sandbox preparation failures stay inside the execution stream.

The workflow canvas now uses a resident ``SandboxSession`` rather than the old
per-run ``RunWorkspace``.  This regression test protects the current ownership
boundary: a failure while preparing the resident run must become a terminal
``EXEC_UPDATE`` and the outer producer must still discard its stop-registry
entry.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import vibecanvas_api.routes.executions as exec_mod


@pytest.mark.asyncio
async def test_produce_execution_prepare_raise_yields_error_and_discards(monkeypatch):
    discarded = {"n": 0}

    async def _boom_prepare(*args, **kwargs):
        raise RuntimeError("materialize blew up")

    async def _persist_noop(*args, **kwargs):
        return None

    # Patch names where the route resolves them.  Workflow validation and the
    # sandbox classification are not under test; preparation fails immediately
    # when the stable /run mount is cleared.
    monkeypatch.setattr(exec_mod, "Workflow", lambda *args, **kwargs: object())
    monkeypatch.setattr(exec_mod, "classify_workflow", lambda wf: "pure")
    monkeypatch.setattr(exec_mod, "clear_run_contents", _boom_prepare)
    monkeypatch.setattr(exec_mod, "_with_execution_repo", _persist_noop)
    monkeypatch.setattr(exec_mod, "_fail_history", _persist_noop)
    monkeypatch.setattr(exec_mod, "create_execution", _persist_noop)

    class _SessionWithoutOwnedClear:
        pass

    class _Manager:
        async def get_session(self, *_args, **_kwargs):
            return _SessionWithoutOwnedClear()

    monkeypatch.setattr(exec_mod, "_acquire_canvas_execution_session", _Manager().get_session)

    def _spy_discard(*a, **k):
        discarded["n"] += 1

    monkeypatch.setattr(exec_mod.stop_registry, "discard", _spy_discard)

    stop = asyncio.Event()
    body = SimpleNamespace(mode="whole", input={}, resume_from=None)
    wf_dict = {
        "__meta__": {"workflow_version": 1},
        "node_1": {"node_id": "node_1", "node_type": "CodeNode"},
    }

    events = []
    async for ev in exec_mod._produce_execution(
            stop, "wf1", "e_pf", body, wf_dict, "u1", "tenant-A"):
        events.append(ev)

    # (a) graceful error event yielded — stream NOT torn / no propagation.
    err = [e for e in events if e[1].get("status") == "error"]
    assert err and "materialize blew up" in err[0][1]["error"]
    # (b) the FD-leak guard still ran on the prepare-raise path.
    assert discarded["n"] == 1
