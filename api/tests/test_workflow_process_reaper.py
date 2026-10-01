"""Process loss closes approval history only after the whole group has exited."""

from contextlib import asynccontextmanager
import json
import os
import signal
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.storage.test_workflow_history import owner, waiting_run
from vibecanvas_api.services import workflow_process_reaper as reaper
from vibecanvas_api.services.sandbox import process_identity as identity
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.workflow_history_repo import HistoryConflict, WorkflowHistoryRepo


def test_process_identity_requires_confirmed_group_exit():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        record = identity.capture_process(process.pid)
        assert not identity.process_group_gone(record)
        assert not identity.process_group_gone({**record, "start": record["start"] - 1})
        assert not identity.process_group_gone({**record, "host_id": "other-host"})
        process.terminate()
        process.wait(timeout=5)
        assert identity.process_group_gone(record)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_group_child_prevents_premature_release():
    # The group leader exits while its child still executes business work.
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('ready',flush=True); sys.stdin.read()",
        ],
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        record = identity.capture_process(process.pid)
        process.stdin.close()
        process.wait(timeout=5)
        assert not identity.process_group_gone(record)
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        process.stdout.close()


def test_inspection_failure_is_not_proof_of_exit(monkeypatch):
    monkeypatch.setattr(identity, "host_identity", lambda: ("host", "boot"))

    def denied(_):
        raise PermissionError("unavailable")

    monkeypatch.setattr(identity, "_stat", denied)
    record = {"host_id": "host", "boot_id": "boot", "pid": 123, "group": 123, "start": 1}
    assert not identity.process_group_gone(record)
    assert identity.process_group_gone({**record, "boot_id": "previous-boot"})


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_lost_process_closes_pending_approval_without_replay(pg_engine, monkeypatch, cancel):
    tenant, actor, _ = await owner()
    run_id, approval_id, _ = await waiting_run(tenant, actor)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)

    @asynccontextmanager
    async def scoped_admin():
        async with short_session_scope(tenant_id=tenant) as db:
            yield db

    monkeypatch.setattr(reaper, "session_scope_admin", scoped_admin)
    try:
        record = identity.capture_process(process.pid)
        async with short_session_scope(tenant_id=tenant) as db:
            await db.execute(
                text("UPDATE workflow_execution_runs SET runtime_process=CAST(:process AS jsonb) WHERE id=:id"),
                {"id": run_id, "process": json.dumps(record)},
            )
            if cancel:
                await WorkflowHistoryRepo(db).request_cancel(run_id)
        await reaper.reap_lost_workflow_processes()
        async with short_session_scope(tenant_id=tenant) as db:
            detail = await WorkflowHistoryRepo(db).detail(run_id)
            assert detail["status"] == "waiting_approval"
            assert "runtime_process" not in detail
        process.terminate()
        process.wait(timeout=5)
        await reaper.reap_lost_workflow_processes()
        await reaper.reap_lost_workflow_processes()  # Idempotent maintenance.
        async with short_session_scope(tenant_id=tenant) as db:
            history = WorkflowHistoryRepo(db)
            detail = await history.detail(run_id)
            assert detail["status"] == ("cancelled" if cancel else "failed")
            assert detail["approvals"][0]["status"] == ("cancelled" if cancel else "execution_lost")
            with pytest.raises(HistoryConflict):
                await history.request_decision(run_id, approval_id, actor_user_id=actor, approved=True)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
