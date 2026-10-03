from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox.gvisor import ServeSnapshot
from vibecanvas_api.services.sandbox.manager import SandboxManager, SandboxSession
from vibecanvas_api.services.sandbox.session_lifecycle import (
    SessionLifecycleState,
    SnapshotKind,
    validate_lifecycle_transition,
)


@pytest.mark.asyncio
async def test_failed_final_writeback_retains_files_and_fences_replacement(tmp_path):
    root = tmp_path / "projection"
    chat = root / "chats" / "conversation"
    chat.mkdir(parents=True)
    private_file = chat / "notes.txt"
    private_file.write_text("not yet persisted")
    session = SandboxSession(tenant_id="tenant", wf_id="workflow", run_dir=str(root),
        overlay_dir=None, provider=MagicMock(), base_binds=[], expose_run=False,
        materialized_projection_root=str(root))
    session._sync_run_folder = AsyncMock(side_effect=OSError("object store unavailable"))
    session._sync_mount_folder = AsyncMock()
    session._stop_agent_runtime_locked = AsyncMock()
    manager = SandboxManager(max_resident=2, idle_ttl_s=10)
    manager._sessions[("tenant", "workflow")] = session
    manager._build_session = AsyncMock()

    result = await manager.close_session("tenant", "workflow")
    assert result["status"] == "releasing"
    assert result["error"] == "sandbox_persistence_incomplete"
    assert private_file.read_text() == "not yet persisted"
    assert not session.closed
    with pytest.raises(RuntimeError, match="sandbox_persistence_incomplete"):
        await manager.get_session("tenant", "workflow")
    manager._build_session.assert_not_awaited()
    session._stop_agent_runtime_locked.assert_awaited_once()

    # The same source survives until a later successful persistence attempt.
    session._sync_run_folder.side_effect = None
    await manager.sweep_idle()
    await manager.drain_background_closes()
    assert session.closed
    assert not root.exists()
    assert not manager._failed_closes
    assert (await manager.status("tenant", "workflow"))["status"] == "closed"


@pytest.mark.asyncio
async def test_slow_close_is_observed_without_cancelling_or_restarting_io(monkeypatch):
    monkeypatch.setattr(config, "sandbox_session_close_timeout_s", 0.1)
    manager = SandboxManager(max_resident=2, idle_ttl_s=10)
    release = asyncio.Event()
    cancelled = []

    async def close():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    session = MagicMock(tenant_id="tenant", wf_id="workflow", closed=False, close=AsyncMock(side_effect=close))
    task = manager._schedule_close(session, reason="test")
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.2)
        assert not task.done()
        assert (await manager.status("tenant", "workflow"))["status"] == "releasing"
        await manager.sweep_idle()
        session.close.assert_awaited_once()
        assert not cancelled
    finally:
        release.set()
        await manager.drain_background_closes()
    assert not manager._failed_closes


@pytest.mark.asyncio
async def test_file_upsert_failure_reaches_strict_writeback(tmp_path, monkeypatch):
    import vibecanvas_api.services.sandbox.manager as mod
    root = tmp_path / "workspace"
    (root / "chats").mkdir(parents=True)
    (root / "chats" / "notes.txt").write_text("keep this")
    session = SandboxSession(tenant_id="tenant", wf_id="workflow", run_dir=str(root),
        overlay_dir=None, provider=MagicMock(), base_binds=[], expose_run=False)
    repository = MagicMock(upsert_artifact_bytes=AsyncMock(side_effect=OSError("storage offline")))

    @asynccontextmanager
    async def transaction(**kwargs):
        yield object()

    monkeypatch.setattr(mod, "short_session_scope", transaction)
    monkeypatch.setattr(mod, "VfsRepo", lambda *args, **kwargs: repository)
    await session.writeback_vfs()  # A failed background sweep does not fail the turn.
    with pytest.raises(RuntimeError, match="sandbox_workspace_persistence_failed"):
        await session.writeback_vfs(strict=True)
    assert (root / "chats" / "notes.txt").read_text() == "keep this"


@pytest.mark.asyncio
async def test_hydration_failure_does_not_start_with_a_partial_workspace(tmp_path, monkeypatch):
    import vibecanvas_api.services.sandbox.manager as mod
    repository = MagicMock(ls=AsyncMock(side_effect=OSError("storage offline")))

    @asynccontextmanager
    async def transaction(**kwargs):
        yield object()

    monkeypatch.setattr(mod, "session_scope", transaction)
    monkeypatch.setattr(mod, "VfsRepo", lambda *args, **kwargs: repository)
    with pytest.raises(OSError, match="storage offline"):
        await mod._hydrate_run_folders(str(tmp_path), "workflow", "tenant")


@pytest.mark.asyncio
async def test_purge_user_storage_removes_daemon_owned_runtime_trees(
    tmp_path,
    monkeypatch,
):
    user_id = str(uuid.uuid4())
    personal_tenant_id = str(uuid.uuid4())
    shared_tenant_id = str(uuid.uuid4())
    other_user_id = str(uuid.uuid4())
    roots = [tmp_path / name for name in ("agent", "overlay", "vfs")]
    monkeypatch.setattr(config, "agent_runtime_root", str(roots[0]))
    monkeypatch.setattr(config, "agent_overlay_root", str(roots[1]))
    monkeypatch.setattr(config, "vfs_volume_root", str(roots[2]))
    for root in roots:
        personal_user = root / personal_tenant_id / user_id
        personal_user.mkdir(parents=True)
        (personal_user / "secret").write_text("erase", encoding="utf-8")
        shared_user = root / shared_tenant_id / user_id
        shared_user.mkdir(parents=True)
        (shared_user / "secret").write_text("erase", encoding="utf-8")
        retained_user = root / shared_tenant_id / other_user_id
        retained_user.mkdir(parents=True)
        (retained_user / "keep").write_text("retain", encoding="utf-8")

    manager = SandboxManager(max_resident=1, idle_ttl_s=60)
    removed = await manager.purge_user_storage(
        user_id,
        [personal_tenant_id, shared_tenant_id],
        personal_tenant_id,
    )

    assert removed is True
    for root in roots:
        assert not (root / personal_tenant_id).exists()
        assert not (root / shared_tenant_id / user_id).exists()
        assert (root / shared_tenant_id / other_user_id / "keep").read_text() == (
            "retain"
        )


@pytest.mark.asyncio
async def test_lazy_create_and_reuse():
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)

    def build(tenant, workspace, user_id=None, expose_run=True, expose_runtime=False, expose_mount=True, workspace_profile="chat"):
        return MagicMock(
            tenant_id=tenant,
            wf_id=workspace,
            closed=False,
            last_used=0.0,
            expose_run=expose_run,
            expose_mount=expose_mount,
            workspace_profile=workspace_profile,
            runtime_dir="/runtime" if expose_runtime else None,
            _requires_rehydrate=False,
            close=AsyncMock(),
        )

    with patch.object(manager, "_build_session", new=AsyncMock(side_effect=build)) as factory:
        first = await manager.get_session("tenant", "chat", expose_run=False)
        second = await manager.get_session("tenant", "chat", expose_run=False)

    assert first is second
    factory.assert_awaited_once()


@pytest.mark.asyncio
async def test_base_fileop_prewarm_uses_no_resident_or_user_session(tmp_path):
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)
    observed: dict[str, object] = {}

    class Pool:
        def submit_fileop(self, request, *, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return {"exit_code": 0, "stdout": '{"ok": true}\n'}

        def stop(self):
            observed["stopped"] = True

    class Session:
        def __init__(self, **kwargs):
            observed["session"] = kwargs
            observed["root"] = os.path.dirname(kwargs["run_dir"])
            self._fileop_pool = None

        async def prewarm_fileops(self):
            self._fileop_pool = Pool()

    with (
        patch(
            "vibecanvas_api.services.sandbox.manager.SandboxSession",
            Session,
        ),
        patch(
            "vibecanvas_api.services.sandbox.manager.get_sandbox_provider",
            return_value=MagicMock(),
        ),
        patch(
            "vibecanvas_api.services.sandbox.manager._workflow_python_binds",
            return_value=[],
        ),
    ):
        result = await manager.prewarm_base_fileops()

    assert result["status"] == "ready"
    assert manager._sessions == {}
    session = observed["session"]
    assert isinstance(session, dict)
    assert session["tenant_id"] == "00000000-0000-0000-0000-000000000000"
    assert session.get("user_id") is None
    assert session.get("runtime_dir") is None
    request = observed["request"]
    assert isinstance(request, dict)
    command = str(request["command"])
    assert '["bs4", "docx", "httpx"' in command
    assert "('bs4', 'docx', 'httpx'" not in command
    assert observed["stopped"] is True
    assert not os.path.exists(str(observed["root"]))


@pytest.mark.asyncio
async def test_session_rebuilds_when_exposed_roots_change():
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)
    sessions = []

    def build(tenant, workspace, user_id=None, expose_run=True, expose_runtime=False, expose_mount=True, workspace_profile="chat"):
        session = MagicMock(
            tenant_id=tenant,
            wf_id=workspace,
            closed=False,
            last_used=0.0,
            expose_run=expose_run,
            expose_mount=expose_mount,
            workspace_profile=workspace_profile,
            runtime_dir="/runtime" if expose_runtime else None,
            close=AsyncMock(),
        )
        sessions.append(session)
        return session

    with patch.object(manager, "_build_session", new=AsyncMock(side_effect=build)):
        first = await manager.get_session("tenant", "chat", expose_run=False)
        second = await manager.get_session("tenant", "chat", expose_run=True)
        third = await manager.get_session(
            "tenant", "chat", expose_run=True, expose_runtime=True)
        fourth = await manager.get_session(
            "tenant", "chat", expose_run=True, expose_runtime=True, expose_mount=False)
        await manager.drain_background_closes()

    assert first is not second and second is not third and third is not fourth
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()
    third.close.assert_awaited_once()
    assert fourth.expose_mount is False


@pytest.mark.asyncio
async def test_manual_close_finishes_before_same_scope_can_be_reacquired():
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    async def close() -> None:
        release_started.set()
        await allow_release.wait()

    session = MagicMock(
        tenant_id="tenant",
        wf_id="chat",
        closed=False,
        close=AsyncMock(side_effect=close),
    )
    manager._sessions[("tenant", "chat")] = session

    closing = asyncio.create_task(manager.close_session("tenant", "chat"))
    await release_started.wait()
    await asyncio.sleep(0)
    assert not closing.done()

    allow_release.set()
    result = await closing

    assert result["status"] == "closed"
    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["idle", "manual"])
async def test_project_reacquire_waits_for_old_cleanup_without_blocking_other_project(reason):
    manager = SandboxManager(max_resident=3, idle_ttl_s=1)
    manager.snapshot_sessions = False
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    async def close():
        release_started.set()
        await allow_release.wait()

    def session(scope):
        return SandboxSession(
            tenant_id="tenant", wf_id=scope, run_dir=None, overlay_dir=None,
            provider=MagicMock(), base_binds=[], expose_run=False,
        )

    old = session("project-a")
    old.close = AsyncMock(side_effect=close)
    old.last_used = time.monotonic() - 10
    manager._sessions[("tenant", "project-a")] = old
    manager._build_session = AsyncMock(side_effect=lambda tenant, scope, **kw: session(scope))
    closing = None
    if reason == "idle":
        assert await manager.sweep_idle() == 1
    else:
        closing = asyncio.create_task(manager.close_session("tenant", "project-a"))
    await asyncio.wait_for(release_started.wait(), 2)
    reacquire = asyncio.create_task(manager.get_session("tenant", "project-a", expose_run=False))
    await asyncio.sleep(0)
    other = await asyncio.wait_for(manager.get_session("tenant", "project-b", expose_run=False), 2)
    assert other.wf_id == "project-b"
    assert not reacquire.done()
    assert manager._build_session.await_count == 1
    allow_release.set()
    fresh = await asyncio.wait_for(reacquire, 2)
    assert fresh is not old
    assert fresh.wf_id == "project-a"
    if closing is not None:
        await closing
    await manager.drain_background_closes()
    assert manager._closing_scopes == {}


def test_session_mounts_only_current_workspace_user_mount_and_runtime(tmp_path):
    run_dir = tmp_path / "workspace"
    mount_dir = tmp_path / "mount"
    runtime_dir = tmp_path / "runtime"
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(run_dir),
        overlay_dir=str(tmp_path / "overlay"),
        provider=MagicMock(),
        base_binds=[],
        mount_dir=str(mount_dir),
        mount_scope_id="__mount_user",
        runtime_dir=str(runtime_dir),
        expose_run=False,
    )

    binds = dict(session._rw_binds)
    assert binds["/data"] == os.path.join(str(run_dir), "data")
    assert binds["/memory"] == os.path.join(str(run_dir), "memory")
    assert binds["/logs"] == os.path.join(str(run_dir), "logs")
    assert binds["/mount"] == str(mount_dir)
    assert binds["/runtime"] == str(runtime_dir)
    assert "/run" not in binds


def test_codex_account_auth_is_not_a_general_session_mount(tmp_path):
    auth_file = tmp_path / "account" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text("{}")
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        runtime_dir=str(tmp_path / "runtime"),
        account_auth_file=str(auth_file),
        expose_run=False,
    )

    # The account file is added dynamically only when the request explicitly
    # selects ``connection_type=chatgpt_account``.
    assert "/runtime/.codex/auth.json" not in dict(session._rw_binds)


@pytest.mark.asyncio
async def test_status_exposes_positive_idle_clock_and_pauses_ttl_while_busy():
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)
    manager.snapshot_sessions = True
    manager.warm_idle_ttl_s = 300
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    manager._sessions[("tenant", "chat")] = session

    session._begin_activity()
    busy = await manager.status("tenant", "chat")
    assert busy["activity_state"] == "busy"
    assert busy["ttl_paused"] is True
    assert busy["ttl_remaining_s"] is None
    assert busy["idle_elapsed_s"] == 0

    session._end_activity()
    session.last_used = time.monotonic() - 12
    idle = await manager.status("tenant", "chat")
    assert idle["activity_state"] == "idle"
    assert idle["ttl_paused"] is False
    assert 11 <= idle["idle_elapsed_s"] <= 13
    assert idle["ttl_s"] == 300
    assert idle["next_transition"] == "hibernate"
    assert idle["resources"]["workspace_projection"] == "released"
    assert idle["resources"]["authentication"] == "detached"


def test_runtime_neutral_lifecycle_contract_rejects_adapter_owned_transitions():
    assert validate_lifecycle_transition("warm", "hibernating") == (
        SessionLifecycleState.WARM,
        SessionLifecycleState.HIBERNATING,
    )
    with pytest.raises(RuntimeError, match="invalid sandbox lifecycle transition"):
        validate_lifecycle_transition("warm", "restoring")


def test_activity_observer_starts_silence_after_abandoned_guest_job_finishes():
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    states = iter([
        {"activity_sequence": 5, "abandoned_jobs": 1},
        {"activity_sequence": 6, "abandoned_jobs": 0},
        {"activity_sequence": 6, "abandoned_jobs": 0},
    ])
    session._fileop_pool = MagicMock()
    session._fileop_pool.activity_snapshot.side_effect = states

    assert session.observe_activity(now=100)["busy"] is True
    assert session.last_used == 100
    assert session.observe_activity(now=110)["busy"] is False
    assert session.last_used == 110
    session.observe_activity(now=115)
    assert session.last_used == 110


@pytest.mark.asyncio
async def test_mirror_vfs_write_targets_mount_and_chat_workspace(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        mount_dir=str(tmp_path / "mount"),
        mount_scope_id="__mount_user",
        expose_run=False,
    )

    assert await session.mirror_vfs_write("/mount/shared.json", b"{}")
    assert await session.mirror_vfs_write("/data/input.txt", b"hello")
    assert (tmp_path / "mount" / "shared.json").read_bytes() == b"{}"
    assert (tmp_path / "workspace" / "data" / "input.txt").read_bytes() == b"hello"
    assert not await session.mirror_vfs_write("/unknown/file", b"x")

    assert await session.mirror_vfs_rename(
        "/data/input.txt", "/data/archive/input.txt",
    )
    assert not (tmp_path / "workspace" / "data" / "input.txt").exists()
    assert (tmp_path / "workspace" / "data" / "archive" / "input.txt").read_bytes() == b"hello"

    assert await session.mirror_vfs_delete("/data/archive")
    assert not (tmp_path / "workspace" / "data" / "archive").exists()


@pytest.mark.asyncio
async def test_mirror_vfs_folder_rename_merges_existing_destination(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    await session.mirror_vfs_write("/data/source/a.txt", b"A")
    await session.mirror_vfs_write("/data/source/nested/b.txt", b"B")
    await session.mirror_vfs_write("/data/destination/existing.txt", b"E")

    assert await session.mirror_vfs_rename("/data/source", "/data/destination")
    root = tmp_path / "workspace" / "data" / "destination"
    assert (root / "a.txt").read_bytes() == b"A"
    assert (root / "nested" / "b.txt").read_bytes() == b"B"
    assert (root / "existing.txt").read_bytes() == b"E"
    assert not (tmp_path / "workspace" / "data" / "source").exists()


@pytest.mark.asyncio
async def test_snapshot_manager_hibernates_then_releases_interactive_session():
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)
    manager.snapshot_sessions = True
    manager.warm_idle_ttl_s = 10
    manager.snapshot_idle_ttl_s = 20
    now = time.monotonic()
    session = MagicMock(
        tenant_id="tenant",
        wf_id="workflow-debug",
        closed=False,
        lease="interactive",
        last_used=now - 11,
        _lifecycle_state="warm",
        _inflight_operations=0,
        close=AsyncMock(),
    )

    async def hibernate() -> bool:
        session._lifecycle_state = "hibernated"
        session._hibernated_at = time.monotonic()
        return True

    session.hibernate = AsyncMock(side_effect=hibernate)
    manager._sessions[("tenant", "workflow-debug")] = session

    assert await manager.sweep_idle() == 0
    session.hibernate.assert_awaited_once()
    assert (await manager.status("tenant", "workflow-debug"))["status"] == "hibernated"

    session._hibernated_at = time.monotonic() - 21
    assert await manager.sweep_idle() == 1
    await manager.drain_background_closes()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_resident_lease_is_not_hibernated_while_work_is_held():
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)
    manager.snapshot_sessions = True
    manager.warm_idle_ttl_s = 1
    session = MagicMock(
        tenant_id="tenant",
        wf_id="chat",
        closed=False,
        lease="resident",
        last_used=time.monotonic() - 100,
        _lifecycle_state="warm",
        _inflight_operations=0,
        hibernate=AsyncMock(),
    )
    manager._sessions[("tenant", "chat")] = session

    assert await manager.sweep_idle() == 0
    session.hibernate.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_session_ttl_starts_only_after_all_activity_ends():
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)
    manager.snapshot_sessions = True
    manager.warm_idle_ttl_s = 1
    session = MagicMock(
        tenant_id="tenant",
        wf_id="workflow-debug",
        closed=False,
        lease="interactive",
        last_used=time.monotonic() - 100,
        _lifecycle_state="warm",
        _inflight_operations=1,
        hibernate=AsyncMock(),
    )
    manager._sessions[("tenant", "workflow-debug")] = session

    assert await manager.sweep_idle() == 0
    session.hibernate.assert_not_awaited()

    session._inflight_operations = 0
    session.last_used = time.monotonic()
    assert await manager.sweep_idle() == 0
    session.hibernate.assert_not_awaited()

    session.last_used = time.monotonic() - 2

    async def hibernate() -> bool:
        session._lifecycle_state = "hibernated"
        session._hibernated_at = time.monotonic()
        return True

    session.hibernate = AsyncMock(side_effect=hibernate)
    assert await manager.sweep_idle() == 0
    session.hibernate.assert_awaited_once()


def _stub_turn_transport(session):
    class Broker:
        def __init__(self):
            self.incoming = asyncio.Queue()

        async def send(self, message):
            turn_id = message["turn_id"]
            if message["type"] == "runtime_request":
                await self.incoming.put({"type": "runtime_event", "turn_id": turn_id,
                                         "event": {"chat_id": turn_id}})
            else:
                await self.incoming.put({"type": "runtime_result", "turn_id": turn_id})

        async def messages(self):
            while True:
                yield await self.incoming.get()

    broker = Broker()
    session._ensure_agent_runtime_locked = AsyncMock(return_value=(broker, True))
    session._mcp_runtime_request = lambda request: request
    session.writeback_vfs = AsyncMock()


def _turn_request(chat_id):
    return {"chat_id": chat_id, "turn_id": chat_id, "runtime_type": "codex"}


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [False, True])
async def test_project_idle_policy_waits_for_concurrent_sibling_chats(snapshot):
    manager = SandboxManager(max_resident=2, idle_ttl_s=10)
    manager.snapshot_sessions = snapshot
    manager.warm_idle_ttl_s = 10
    session = SandboxSession(
        tenant_id="tenant", wf_id="project", run_dir=None, overlay_dir=None,
        provider=MagicMock(), base_binds=[], expose_run=False,
    )
    manager._sessions[("tenant", "project")] = session
    session.close = AsyncMock()

    async def hibernate():
        session._lifecycle_state = "hibernated"
        session._hibernated_at = time.monotonic()
        return True

    session.hibernate = AsyncMock(side_effect=hibernate)

    _stub_turn_transport(session)
    first = session.run_agent_runtime_stream(_turn_request("chat-a"))
    second = session.run_agent_runtime_stream(_turn_request("chat-b"))
    assert (await anext(first))["chat_id"] == "chat-a"
    assert (await asyncio.wait_for(anext(second), 1))["chat_id"] == "chat-b"
    assert session._inflight_operations == 2
    session.last_used = time.monotonic() - 100
    assert await manager.sweep_idle() == 0
    assert (await manager.status("tenant", "project"))["ttl_paused"] is True

    await first.aclose()
    assert session._inflight_operations == 1
    session.last_used = time.monotonic() - 100
    assert await manager.sweep_idle() == 0
    session.close.assert_not_awaited()
    session.hibernate.assert_not_awaited()

    await second.aclose()
    assert session._inflight_operations == 0
    idle = await manager.status("tenant", "project")
    assert idle["ttl_paused"] is False
    assert idle["idle_elapsed_s"] < 1
    assert await manager.sweep_idle() == 0
    # Reading status must not reset the countdown.
    session.last_used = time.monotonic() - 11
    assert (await manager.status("tenant", "project"))["idle_elapsed_s"] >= 11
    assert await manager.sweep_idle() == (0 if snapshot else 1)
    await manager.drain_background_closes()
    if snapshot:
        session.hibernate.assert_awaited_once()
    else:
        session.close.assert_awaited_once()
    await session._runtime_router.close()


@pytest.mark.asyncio
async def test_cancelled_queued_chat_does_not_unpin_running_project_sibling():
    session = SandboxSession(
        tenant_id="tenant", wf_id="project", run_dir=None, overlay_dir=None,
        provider=MagicMock(), base_binds=[], expose_run=False,
    )

    _stub_turn_transport(session)
    first = session.run_agent_runtime_stream(_turn_request("chat-a"))
    second = session.run_agent_runtime_stream(_turn_request("chat-b"))
    await anext(first)
    await session._lock.acquire()  # B is queued only for shared setup.
    waiting = asyncio.create_task(anext(second))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    session._lock.release()
    assert session._inflight_operations == 1
    assert session.observe_activity()["busy"] is True
    await first.aclose()
    assert session._inflight_operations == 0
    assert session.observe_activity()["busy"] is False
    await session._runtime_router.close()


@pytest.mark.asyncio
async def test_session_snapshot_restores_same_fileop_pool(monkeypatch, tmp_path):
    class Pool:
        size = 2

        def activity_snapshot(self):
            return {}

        def __init__(self) -> None:
            self.restored: ServeSnapshot | None = None

        def checkpoint(
            self,
            *,
            image_dir: str,
            fingerprint: str,
            kind: str,
        ) -> ServeSnapshot:
            os.makedirs(image_dir)
            with open(os.path.join(image_dir, "checkpoint.img"), "wb") as image:
                image.write(b"snapshot")
            return ServeSnapshot(
                image_dir=image_dir,
                fingerprint=fingerprint,
                kind=kind,
            )

        def restore(self, snapshot: ServeSnapshot) -> None:
            self.restored = snapshot

        def is_quiescent(self) -> bool:
            return True

    snapshot_root = tmp_path / "snapshots"
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.manager.config.sandbox_resident_mode",
        "snapshot",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.manager.config.sandbox_snapshot_root",
        str(snapshot_root),
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.manager.config.sandbox_snapshot_max_count",
        8,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.manager.config.sandbox_snapshot_max_bytes",
        1024 * 1024,
    )
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="workflow-debug",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=True,
    )
    pool = Pool()
    session._fileop_pool = pool
    session.writeback_vfs = AsyncMock()
    session._stop_agent_runtime_locked = AsyncMock()

    assert await session.hibernate() is True
    assert session._lifecycle_state == "hibernated"
    assert session._serve_snapshot is not None
    assert session._serve_snapshot.kind == SnapshotKind.SESSION_HIBERNATION
    assert session.resource_status()["vfs_mount"] == "detached"
    assert session.resource_status()["snapshot_kind"] == "session_hibernation"
    session.writeback_vfs.assert_awaited_once()

    assert await session.resume() is True
    assert session._lifecycle_state == "warm"
    assert pool.restored is session._serve_snapshot


@pytest.mark.asyncio
async def test_codex_account_disconnect_invalidates_hibernated_binding():
    manager = SandboxManager(max_resident=8, idle_ttl_s=600)
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        user_id="user",
        expose_run=False,
    )
    session._bound_runtime_type = "codex"
    session._bound_runtime_uses_codex_account = True
    session._runtime_uses_codex_account = False
    session._lifecycle_state = "hibernated"
    session._hibernated_at = time.monotonic()
    session.close = AsyncMock()
    manager._sessions[("tenant", "chat")] = session

    assert await manager.invalidate_codex_account_sessions("tenant", "user") == 1
    await manager.drain_background_closes()
    session.close.assert_awaited_once()
    assert ("tenant", "chat") not in manager._sessions


@pytest.mark.asyncio
async def test_hibernate_never_changes_state_while_operation_is_inflight(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.manager.config.sandbox_resident_mode",
        "snapshot",
    )
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    session._inflight_operations = 1

    assert await session.hibernate() is False
    assert session._lifecycle_state == "warm"


def test_new_activity_cannot_race_a_hibernating_session(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    session._lifecycle_state = "hibernating"

    with pytest.raises(RuntimeError, match="reacquire"):
        session._begin_activity()
    assert session._inflight_operations == 0


@pytest.mark.asyncio
async def test_external_vfs_commit_is_atomic_and_fenced_from_writeback(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    path = "/data/diagrams/system.drawio"
    assert await session.acknowledge_external_vfs_commit(path, b"presented")
    committed = (
        tmp_path / "workspace" / "data" / "diagrams" / "system.drawio"
    )
    assert committed.read_bytes() == b"presented"
    assert path in session._external_vfs_fenced_paths
    assert session._requires_rehydrate is False


@pytest.mark.asyncio
async def test_external_vfs_path_is_fenced_before_host_commit(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    path = "/data/diagrams/system.drawio"

    # Platform MCP execution may already own the main session lock. The VFS
    # fence uses its dedicated lock and must remain non-reentrant-safe here.
    async with session._lock:
        assert await asyncio.wait_for(
            session.fence_external_vfs_path(path),
            timeout=0.1,
        )
    assert path in session._external_vfs_fenced_paths
    assert session._requires_rehydrate is False


@pytest.mark.asyncio
async def test_external_vfs_commit_failure_requires_rehydrate(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    path = "/data/diagrams/system.drawio"
    assert not await session.acknowledge_external_vfs_commit(path, b"presented")
    assert path in session._external_vfs_fenced_paths
    assert session._requires_rehydrate is True


@pytest.mark.asyncio
async def test_completed_file_tool_writes_exact_path_through_to_vfs(tmp_path):
    workspace = tmp_path / "workspace"
    target = workspace / "data" / "diagram.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'{"ready":true}')
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(workspace),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    repo = MagicMock()
    repo.upsert_artifact_bytes = AsyncMock(return_value=False)

    @asynccontextmanager
    async def fake_scope(*, tenant_id):
        assert tenant_id == "tenant"
        yield MagicMock()

    with (
        patch(
            "vibecanvas_api.services.sandbox.manager.short_session_scope",
            fake_scope,
        ),
        patch(
            "vibecanvas_api.services.sandbox.manager.VfsRepo",
            return_value=repo,
        ),
        patch(
            "vibecanvas_api.services.sandbox.manager.get_object_store",
            return_value=MagicMock(),
        ),
    ):
        assert await session.sync_workspace_path("/data/diagram.json")

    repo.upsert_artifact_bytes.assert_awaited_once_with(
        wf_id="chat",
        tenant="tenant",
        path="/data/diagram.json",
        data=b'{"ready":true}',
        content_type="application/json",
    )
    assert not await session.sync_workspace_path("/data/../memory/secret.md")


@pytest.mark.asyncio
async def test_runtime_tool_end_persists_before_projection_is_yielded(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=False,
    )
    session.sync_workspace_path = AsyncMock(return_value=True)
    session.writeback_vfs = AsyncMock(return_value=0)

    def event(tool_name, tool_input):
        return {
            "type": "projection",
            "payload": {
                "event_type": "CHAT_EVENT",
                "payload": {
                    "type": "tool_end",
                    "status": "done",
                    "invocation": {"name": tool_name, "input": tool_input},
                },
            },
        }

    await session._write_through_runtime_tool_event(
        event("write_file", {"path": "/memory/state.md"})
    )
    session.sync_workspace_path.assert_awaited_once_with("/memory/state.md")

    for command_tool in (
        "bash",
        "exec_command",
        "apply_patch",
        "shell",
        "file_change",
    ):
        await session._write_through_runtime_tool_event(
            event(command_tool, {"command": "true"})
        )
    assert session.writeback_vfs.await_count == 5


@pytest.mark.asyncio
async def test_session_run_code_uses_provider_and_writes_back(tmp_path):
    provider = MagicMock()
    result = MagicMock(
        final_outputs={"stdout": "ok", "stderr": "", "exit_code": 0},
        error_dict={},
    )
    provider.run_code.return_value = result
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        expose_run=False,
    )
    session.writeback_vfs = AsyncMock(return_value=0)

    out = await session.run_code(script="print('ok')", inputs={}, timeout_s=30.0)

    assert out["stdout"] == "ok"
    provider.run_code.assert_called_once()
    session.writeback_vfs.assert_awaited_once()


@pytest.mark.asyncio
async def test_resident_job_applies_host_policy_before_guest_submission(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="workflow",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=True,
    )
    pool = MagicMock()
    pool.acquire_egress_hosts.return_value = "lease-1"
    pool.submit_sandbox_job.return_value = {"status": "success"}
    session._get_fileop_pool = AsyncMock(return_value=pool)

    result = await session.submit_sandbox_job(
        {
            "kind": "workflow",
            "run_id": "run",
            "_allow_hosts": ["api.example", "files.example"],
        },
        timeout=30.0,
    )

    assert result == {"status": "success"}
    pool.acquire_egress_hosts.assert_called_once_with(
        ["api.example", "files.example"]
    )
    pool.release_egress_hosts.assert_called_once_with("lease-1")
    submitted = pool.submit_sandbox_job.call_args.args[0]
    assert "_allow_hosts" not in submitted


@pytest.mark.asyncio
async def test_resident_job_releases_host_policy_when_submission_fails(tmp_path):
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="workflow",
        run_dir=str(tmp_path / "workspace"),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=True,
    )
    pool = MagicMock()
    pool.acquire_egress_hosts.return_value = "lease-error"
    pool.submit_sandbox_job.side_effect = RuntimeError("worker failed")
    session._get_fileop_pool = AsyncMock(return_value=pool)

    with pytest.raises(RuntimeError, match="worker failed"):
        await session.submit_sandbox_job(
            {"kind": "workflow", "_allow_hosts": ["api.example"]},
            timeout=30.0,
        )

    pool.release_egress_hosts.assert_called_once_with("lease-error")


@pytest.mark.asyncio
async def test_submit_node_job_stages_in_requested_workflow_run(tmp_path):
    """A Chat session may execute a selected Workflow whose /run id differs
    from the Chat workspace id. Staging and result lookup must use the same
    validated Workflow subpath exposed to the sandbox worker."""
    chat_dir = tmp_path / "chat-workspace"
    chat_dir.mkdir()
    workflow_dir = tmp_path / "selected-workflow"
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat-id",
        run_dir=str(chat_dir),
        overlay_dir=None,
        provider=MagicMock(),
        base_binds=[],
        expose_run=True,
    )

    async def submit(job, *, timeout):
        assert job["run_subpath"] == "selected-workflow"
        exec_dir = workflow_dir / "__exec__"
        assert (exec_dir / "job.json").exists()
        (exec_dir / "result.json").write_text(
            '{"final_outputs":{"node_1":{"x":3}},"error_dict":{}}',
            encoding="utf-8",
        )
        return {"status": "success"}

    session.submit_sandbox_job = AsyncMock(side_effect=submit)
    session.writeback_vfs = AsyncMock(return_value=0)
    response = await session.submit_node_job(
        node={"node_id": "node_1", "node_type": "StartNode"},
        inputs={"x": 3},
        extra=None,
        tenant="tenant",
        run_id="selected-workflow",
        run_subpath="selected-workflow",
        timeout=30.0,
    )

    assert response["result"]["final_outputs"]["node_1"] == {"x": 3}
    assert not (chat_dir / "__exec__" / "job.json").exists()
    session.writeback_vfs.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_sweep_maintains_resident_skill_views_without_touching_activity():
    manager = SandboxManager(max_resident=1, idle_ttl_s=60)
    session = SandboxSession(tenant_id='tenant', wf_id='resident', run_dir=None,
        overlay_dir=None, provider=MagicMock(), base_binds=[], expose_run=False)
    session.lease = 'resident'
    session.maintain_workflow_skills = AsyncMock()
    last_used = session.last_used
    manager._sessions[('tenant', 'resident')] = session
    assert await manager.sweep_idle() == 0
    session.maintain_workflow_skills.assert_awaited_once()
    assert session.last_used == last_used


@pytest.mark.asyncio
async def test_execution_lease_prevents_eviction_between_rpc_calls():
    manager = SandboxManager(max_resident=1, idle_ttl_s=600)
    protected = SandboxSession(tenant_id='tenant', wf_id='schedule', run_dir=None,
        overlay_dir=None, provider=MagicMock(), base_binds=[], expose_run=True)
    protected.lease = 'resident'
    protected.close = AsyncMock()
    manager._sessions[('tenant', 'schedule')] = protected
    manager._build_session = AsyncMock()
    with pytest.raises(RuntimeError, match='capacity is full'):
        await manager.get_session('tenant', 'deployment')
    assert await manager.get_loaded_session('tenant', 'schedule') is protected
    protected.close.assert_not_awaited()
    manager._build_session.assert_not_awaited()
    await manager.close_session('tenant', 'schedule')
    assert await manager.get_loaded_session('tenant', 'schedule') is None
