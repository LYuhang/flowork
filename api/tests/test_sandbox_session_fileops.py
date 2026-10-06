# -*- coding: utf-8 -*-
"""@gvisor — Task 4b-ii: the warm-backed file API on ``SandboxSession``.

A resident :class:`SandboxSession` lazily holds a no-DB ``WarmGvisorPool`` whose
network posture follows SANDBOX_NETWORK and whose worker mounts the agent's clean file roots
(``/data /memory /logs /mount``) and serves file ops INSIDE the sandbox. The
five async methods (``read_file``/``write_file``/``list_dir``/``grep``/
``edit_file``) submit ops off-thread and return the raw result dicts.

This boots a REAL warm worker (~20-30s), so it mirrors the sibling @gvisor
harness (filesystem object store monkeypatch + skip guard from
``test_agent_sandbox_e2e.py``). The ops run IN the sandbox — the host file under
``run_dir/data/x.txt`` (the shared mount) reflects every write.
"""
from __future__ import annotations

import base64
import os
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.config import config as _config
from vibecanvas_api.services.sandbox import _gvisor_runnable
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

gvisor = pytest.mark.skipif(
    not _gvisor_runnable(), reason="rootless gVisor not runnable here")


@pytest.fixture
def _fs_object_store(monkeypatch, tmp_path):
    """Point the object store + staging/overlay roots at real tmp dirs so
    ``build_run_context`` materializes a REAL ``run_dir`` (InMemory degrades it
    to ``None``)."""
    monkeypatch.setattr(_config.object_store, "provider", "filesystem")
    monkeypatch.setattr(_config.object_store, "fs_root", str(tmp_path / "objstore"))
    monkeypatch.setattr(_config, "agent_overlay_root", str(tmp_path / "overlay"))
    monkeypatch.setattr(_config, "kms_provider", "local")
    monkeypatch.setattr(
        _config,
        "kms_local_master_key",
        base64.urlsafe_b64encode(b"f" * 32).decode(),
    )
    monkeypatch.setattr(_config, "kms_local_master_key_file", "")


async def _seed_committed_workflow() -> tuple[str, str]:
    """Seed tenant + user + workflow via a COMMITTED ``session_scope`` so the
    manager's own transaction sees the rows. Returns ``(tenant_hex, wf_id)``."""
    t, u = uuid.uuid4(), uuid.uuid4()
    tenant_hex = t.hex
    async with session_scope(tenant_id=str(t)) as s:
        await s.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                        {"t": t})
        await s.execute(
            text("INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)"),
            {"u": u, "t": t, "e": f"{u.hex[:6]}@example.com"})
        wf = await WorkflowRepo(s, str(u)).create_workflow(name="W")
        wf_id = wf["wf_id"]
    return tenant_hex, wf_id


@gvisor
@pytest.mark.gvisor
@pytest.mark.asyncio
async def test_session_warm_file_api_roundtrip(_fs_object_store):
    tenant_id, wf_id = await _seed_committed_workflow()

    token = current_sync_tenant_id.set(tenant_id)
    session = None
    try:
        mgr = get_sandbox_manager()
        session = await mgr.get_session(tenant_id, wf_id)

        # write → read roundtrip, op runs IN the sandbox warm worker.
        w = await session.write_file("/data/x.txt", "hi")
        assert w["ok"] is True, w

        r = await session.read_file("/data/x.txt")
        assert r == {"ok": True, "kind": "text", "content": "hi"}, r

        w_special = await session.write_file("/data/PBR_MP[GB].md", "# report\n")
        assert w_special["ok"] is True, w_special
        r_special = await session.read_file("/data/PBR_MP[GB].md")
        assert r_special == {"ok": True, "kind": "text", "content": "# report\n"}, r_special

        # bash and file APIs share the same resident worker view.
        cmd = await session.run_command("printf worker > /data/from_bash.txt", timeout_s=30)
        assert cmd["exit_code"] == 0, cmd
        r_cmd = await session.read_file("/data/from_bash.txt")
        assert r_cmd == {"ok": True, "kind": "text", "content": "worker"}, r_cmd

        # list shows the file
        ls = await session.list_dir("/data")
        assert ls["ok"] is True, ls
        names = [e["name"] for e in ls["entries"]]
        assert "x.txt" in names, ls

        # grep finds it
        g = await session.grep("hi", "/data")
        assert g["ok"] is True, g
        assert any("/data/x.txt:1:hi" in m for m in g["matches"]), g

        # edit: unique replacement (+ a line-numbered unified diff of the change)
        e = await session.edit_file("/data/x.txt", "hi", "bye")
        assert e["ok"] is True and e["replacements"] == 1, e
        assert "   1 -\thi" in e["diff"] and "   1 +\tbye" in e["diff"], e
        assert "@@" in e["diff"], e                              # hunk header retained

        r2 = await session.read_file("/data/x.txt")
        assert r2 == {"ok": True, "kind": "text", "content": "bye"}, r2

        # The op ran IN the sandbox via the SHARED mount — the host file reflects it.
        host_file = os.path.join(session.run_dir, "data", "x.txt")
        with open(host_file, "r", encoding="utf-8") as f:
            assert f.read() == "bye"
    finally:
        current_sync_tenant_id.reset(token)
        if session is not None:
            await session.close()


@gvisor
@pytest.mark.gvisor
@pytest.mark.asyncio
async def test_session_edit_not_unique(_fs_object_store):
    tenant_id, wf_id = await _seed_committed_workflow()

    token = current_sync_tenant_id.set(tenant_id)
    session = None
    try:
        mgr = get_sandbox_manager()
        session = await mgr.get_session(tenant_id, wf_id)

        await session.write_file("/data/dup.txt", "aa")
        # "a" occurs twice and replace_all is False → not_unique.
        e = await session.edit_file("/data/dup.txt", "a", "b")
        assert e == {"ok": False, "error": "not_unique"}, e

        # not_found when old absent.
        e2 = await session.edit_file("/data/dup.txt", "zzz", "b")
        assert e2 == {"ok": False, "error": "not_found"}, e2

        # replace_all clears the uniqueness requirement.
        e3 = await session.edit_file("/data/dup.txt", "a", "b", replace_all=True)
        assert e3["ok"] is True and e3["replacements"] == 2, e3
        assert "diff" in e3, e3
    finally:
        current_sync_tenant_id.reset(token)
        if session is not None:
            await session.close()


@gvisor
@pytest.mark.gvisor
@pytest.mark.asyncio
async def test_session_grep_context_and_glob(_fs_object_store):
    """grep gathers ±context lines IN the sandbox (match line uses ``:``, context
    uses ``-``) and filters files by ``glob``; ``match_count`` counts real matches."""
    tenant_id, wf_id = await _seed_committed_workflow()

    token = current_sync_tenant_id.set(tenant_id)
    session = None
    try:
        mgr = get_sandbox_manager()
        session = await mgr.get_session(tenant_id, wf_id)

        await session.write_file("/data/a.md", "line1\nNEEDLE here\nline3\n")
        await session.write_file("/data/b.txt", "NEEDLE other\n")

        # context=1 → the match line (`:`) plus one line before/after (`-`).
        g = await session.grep("NEEDLE", "/data", context=1)
        assert g["ok"] and g["match_count"] == 2, g
        joined = "\n".join(g["matches"])
        assert "/data/a.md:2:NEEDLE here" in joined        # match line, ':' separator
        assert "/data/a.md-1-line1" in joined              # context before, '-' separator
        assert "/data/a.md-3-line3" in joined              # context after

        # glob '*.md' restricts the walk to the markdown file.
        g2 = await session.grep("NEEDLE", "/data", glob="*.md")
        assert g2["match_count"] == 1, g2
        assert all(m.startswith("/data/a.md") for m in g2["matches"]), g2
    finally:
        current_sync_tenant_id.reset(token)
        if session is not None:
            await session.close()


@gvisor
@pytest.mark.gvisor
@pytest.mark.asyncio
async def test_session_read_write_bytes_roundtrip(_fs_object_store):
    """write_bytes/read_bytes carry RAW bytes (incl. NUL / every byte value) through
    the sandbox via base64 transport — the binary path the data tools need for xlsx."""
    tenant_id, wf_id = await _seed_committed_workflow()

    token = current_sync_tenant_id.set(tenant_id)
    session = None
    try:
        mgr = get_sandbox_manager()
        session = await mgr.get_session(tenant_id, wf_id)

        raw = bytes(range(256)) * 8            # 2048 bytes, every byte value incl NUL
        w = await session.write_bytes("/data/blob.bin", raw)
        assert w["ok"] and w["bytes"] == len(raw), w

        r = await session.read_bytes("/data/blob.bin")
        assert r["ok"] and r["data"] == raw, (r.get("ok"), len(r.get("data", b"")))

        # missing file → clean not_found (no crash)
        miss = await session.read_bytes("/data/nope.bin")
        assert miss == {"ok": False, "error": "not_found"}, miss
    finally:
        current_sync_tenant_id.reset(token)
        if session is not None:
            await session.close()


@gvisor
@pytest.mark.gvisor
@pytest.mark.asyncio
async def test_session_reads_run_tier_results(_fs_object_store):
    """The warm worker mounts the whole run_dir at /run, so the agent reads
    run-tier results (/run/__exec__/nodes/<id>.json) IN the sandbox."""
    tenant_id, wf_id = await _seed_committed_workflow()

    token = current_sync_tenant_id.set(tenant_id)
    session = None
    try:
        mgr = get_sandbox_manager()
        session = await mgr.get_session(tenant_id, wf_id)

        # Seed a run-tier result on the HOST (the run_dir bound at /run).
        node = os.path.join(session.run_dir, "__exec__", "nodes", "x.json")
        os.makedirs(os.path.dirname(node), exist_ok=True)
        with open(node, "w", encoding="utf-8") as f:
            f.write('{"ok":1}')

        r = await session.read_file("/run/__exec__/nodes/x.json")
        assert r == {"ok": True, "kind": "text", "content": '{"ok":1}'}, r

        # Channel-exposure guard: the staging (inbox/outbox + /runs store) moved
        # to a SIBLING of run_dir, so /run/.fileops is NOT reachable. The path
        # resolves under the /run root but the file isn't there → an ERROR result
        # (not readable content), never the channel inbox.
        bad = await session.read_file("/run/.fileops/work/inbox")
        assert bad.get("ok") is False, bad
        assert bad.get("error") in ("not_found", "path_outside_roots"), bad
    finally:
        current_sync_tenant_id.reset(token)
        if session is not None:
            await session.close()
