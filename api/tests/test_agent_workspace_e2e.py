# -*- coding: utf-8 -*-
"""F2.5/F1 — @gvisor END-TO-END: the new agent-WORKSPACE capabilities on a REAL
resident gVisor sandbox session.

This is the workspace counterpart to ``test_agent_sandbox_e2e.py`` (F0+F2). It
exercises the three coding-agent affordances that ride the SAME resident
``SandboxSession`` (built via ``get_sandbox_manager().get_session``):

  * **F2.5 install→import** — ``run_install(spec, manager="pip")`` opens egress
    (``network="host"``) and pip-installs the package to the per-wf overlay
    ``/opt/agent-overlay/py`` (on PYTHONPATH); a SECOND ``run_code`` then
    imports it. We FIRST prove (in a clean ``run_code``) the chosen package is
    NOT importable from the host base, so a green import is attributable to the
    overlay install (not a base-image freebie).
  * **F1 run_command write→VFS** — ``run_command("echo ... > /data/note.txt")``
    writes through the clean top-level workspace folder.
    exits 0 and the file surfaces in the durable VFS at ``/data/note.txt`` via
    the post-command write-back (read back with ``VfsRepo.read_bytes``).
  * **F1 persistent workspace** — a SECOND ``run_command("cat /data/note.txt")``
    in the SAME session returns the content, proving the live run_dir persists
    across ``run_command`` calls (no FS-tool wiring needed).

Skip-clean MIRRORING the sibling @gvisor guard
(``pytest.mark.skipif(not _gvisor_runnable())``) — runs where rootless gVisor
boots, skips cleanly where it cannot. The tenant/VFS seeding + FS object store
fixture are copied verbatim from ``test_agent_sandbox_e2e.py`` so
``build_run_context`` materializes a REAL ``run_dir`` (InMemory degrades it to
``None``).
"""
from __future__ import annotations

import base64
import os
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.config import config as _config
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.sandbox import _gvisor_runnable
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.vfs_store import VfsRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

# Mirror the sibling @gvisor guard exactly: skip cleanly if rootless gVisor
# can't boot here. The ``gvisor`` marker is ALSO applied so ``pytest -m gvisor``
# selects this test.
gvisor = pytest.mark.skipif(
    not _gvisor_runnable(), reason="rootless gVisor not runnable here")
external_network = pytest.mark.skipif(
    os.environ.get("FLOWORK_TEST_NETWORK") != "1",
    reason="set FLOWORK_TEST_NETWORK=1 to run external package-registry checks",
)

# A small PURE-PYTHON package that is NOT in the host base image (we assert it
# is un-importable in a clean run BEFORE installing). Chosen for being tiny +
# dependency-light so a network pip install over the F0-proven egress is fast.
_PKG = "cowsay"
_IMPORT_NAME = "cowsay"


@pytest.fixture
def _fs_object_store(monkeypatch, tmp_path):
    """Point the configured object store + staging/overlay roots at real
    tmp_path dirs so ``build_run_context`` materializes a REAL ``run_dir``
    (InMemory degrades it to ``None`` and the run-dir write-back would have no
    host dir to read). Copied verbatim from ``test_agent_sandbox_e2e.py``.
    """
    monkeypatch.setattr(_config.object_store, "provider", "filesystem")
    monkeypatch.setattr(_config.object_store, "fs_root", str(tmp_path / "objstore"))
    monkeypatch.setattr(_config, "agent_overlay_root", str(tmp_path / "overlay"))
    monkeypatch.setattr(_config, "kms_provider", "local")
    monkeypatch.setattr(
        _config,
        "kms_local_master_key",
        base64.urlsafe_b64encode(b"w" * 32).decode(),
    )
    monkeypatch.setattr(_config, "kms_local_master_key_file", "")


async def _seed_committed_workflow() -> tuple[str, str]:
    """Seed tenant + user + workflow via a COMMITTED ``session_scope`` (NOT the
    rolled-back ``pg_session``) so the manager's own separate transaction sees
    the rows. Returns ``(tenant_hex, wf_id)``. Mirrors
    ``test_agent_sandbox_e2e.py`` (this test writes its own files at runtime).
    """
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


# Clean-room probe: try to import the chosen package and report. Exit 0 + the
# OK marker only if importable; otherwise non-zero + the absence marker.
_PROBE_SCRIPT = f"""
import sys
try:
    import {_IMPORT_NAME}
    print("IMPORT_OK")
except Exception as exc:
    print("IMPORT_MISSING: %r" % (exc,))
    sys.exit(7)
"""


@gvisor
@external_network
@pytest.mark.gvisor
@pytest.mark.asyncio
async def test_install_package_then_import(_fs_object_store):
    """F2.5: a package NOT in the base installs into the overlay and then
    imports inside the same session."""
    tenant_id, wf_id = await _seed_committed_workflow()

    token = current_sync_tenant_id.set(tenant_id)
    try:
        mgr = get_sandbox_manager()
        session = await mgr.get_session(tenant_id, wf_id)

        # 1. PROVE the package is NOT importable from the host base (so a later
        #    green import is attributable to the overlay install, not a freebie).
        before = await session.run_code(_PROBE_SCRIPT, {}, timeout_s=60)
        assert before["exit_code"] != 0, (
            f"{_PKG!r} was ALREADY importable from the base — pick a package the "
            f"base lacks. stdout={before['stdout']!r}")
        assert "IMPORT_MISSING" in before["stdout"], before["stdout"]

        # 2. Install it into the per-wf overlay (network=host egress).
        inst = await session.run_install(_PKG, manager="pip", timeout_s=180)
    finally:
        current_sync_tenant_id.reset(token)

    # Package registries are intentionally external to the test environment.
    # Keep this as a real network integration check when egress is available,
    # but do not turn an offline/proxy-only CI runner into a product failure.
    if inst["exit_code"] != 0:
        transport_error = inst["stderr"].lower()
        if any(marker in transport_error for marker in (
            "temporary failure in name resolution",
            "connection timed out",
            "read timed out",
            "network is unreachable",
            "proxyerror",
        )):
            pytest.skip("package registry is unavailable from this test environment")

    assert inst["exit_code"] == 0, (
        f"pip install {_PKG!r} did not succeed (exit={inst['exit_code']}). If "
        f"this env blocks egress even with network=host, that is an ENV "
        f"limitation, not a code bug. stdout={inst['stdout'][-800:]!r} "
        f"stderr={inst['stderr'][-800:]!r}")

    # 3. Now the freshly-installed package imports (overlay /opt/agent-overlay/py
    #    is on PYTHONPATH).
    token = current_sync_tenant_id.set(tenant_id)
    try:
        session = await mgr.get_session(tenant_id, wf_id)
        after = await session.run_code(_PROBE_SCRIPT, {}, timeout_s=60)
    finally:
        current_sync_tenant_id.reset(token)

    assert after["exit_code"] == 0, (
        f"import {_IMPORT_NAME!r} failed AFTER install — overlay PYTHONPATH wiring "
        f"broken. stdout={after['stdout']!r} stderr={after['stderr'][-800:]!r}")
    assert "IMPORT_OK" in after["stdout"], after["stdout"]


@gvisor
@pytest.mark.gvisor
@pytest.mark.asyncio
async def test_run_command_write_surfaces_in_vfs_and_persists(_fs_object_store):
    """F1: run_command writes a file into the run-dir /data folder, the post-
    command write-back surfaces it in the durable VFS at /data/note.txt, and a
    SECOND run_command in the same session reads it back (live run_dir persists).
    """
    tenant_id, wf_id = await _seed_committed_workflow()

    token = current_sync_tenant_id.set(tenant_id)
    try:
        mgr = get_sandbox_manager()
        session = await mgr.get_session(tenant_id, wf_id)

        # /data is the clean user-visible workspace root. mkdir -p in case the
        # backing folder was not pre-created.
        write = await session.run_command(
            "mkdir -p /data && echo workspace-ok > /data/note.txt",
            timeout_s=60)
        assert write["exit_code"] == 0, (
            f"write run_command failed: exit={write['exit_code']} "
            f"stderr={write['stderr'][-800:]!r}")

        # SECOND command in the SAME session reads the file back — proves the
        # live run_dir persists across run_command calls.
        read = await session.run_command("cat /data/note.txt", timeout_s=60)
        assert read["exit_code"] == 0, (
            f"cat run_command failed: exit={read['exit_code']} "
            f"stderr={read['stderr'][-800:]!r}")
        assert "workspace-ok" in read["stdout"], read["stdout"]
    finally:
        current_sync_tenant_id.reset(token)

    # The write-back surfaced /data/note.txt in the durable VFS at /data/.
    async with session_scope(tenant_id=tenant_id) as s:
        repo = VfsRepo(s, object_store=get_object_store())
        body = await repo.read_bytes(wf_id=wf_id, path="/data/note.txt")
    assert body is not None, "/data/note.txt did not surface in the VFS"
    assert b"workspace-ok" in body, body
