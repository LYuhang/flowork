from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.sandbox.manager import SandboxManager, SandboxSession


@pytest.fixture
def manager(tmp_path):
    manager = SandboxManager(max_resident=1, idle_ttl_s=10)
    manager.snapshot_sessions = False

    async def binding(tenant_id, scope_id, user_id):
        return "workflow" if scope_id.startswith("__projectws_") else None

    async def build(tenant_id, scope_id, *, user_id=None, expose_run=True,
                    expose_runtime=False, **kwargs):
        root = tmp_path / scope_id
        root.mkdir(exist_ok=True)
        session = SandboxSession(
            tenant_id=tenant_id, wf_id=scope_id, user_id=user_id,
            run_dir=str(root), overlay_dir=None, provider=MagicMock(), base_binds=[],
            runtime_dir=str(root / "runtime") if expose_runtime else None,
            expose_run=expose_run, materialized_projection_root=str(root),
        )
        session.writeback_vfs = AsyncMock()
        session._stop_agent_runtime_locked = AsyncMock()
        return session

    manager._workflow_chat_owner = AsyncMock(side_effect=binding)
    manager._build_session = AsyncMock(side_effect=build)
    return manager


@pytest.mark.asyncio
async def test_run_and_two_users_share_lifecycle_but_not_private_mounts(manager):
    a_scope = project_workspace_scope_id("private-a")
    b_scope = project_workspace_scope_id("private-b")
    parent, a, b, a_again = await asyncio.gather(
        manager.get_session("tenant", "workflow", user_id="alice"),
        manager.get_session("tenant", a_scope, user_id="alice", expose_runtime=True),
        manager.get_session("tenant", b_scope, user_id="bob", expose_runtime=True),
        manager.get_session("tenant", a_scope, user_id="alice", expose_runtime=True),
    )
    assert a_again is a
    assert a._workflow_parent is b._workflow_parent is parent
    assert list(manager._sessions) == [("tenant", "workflow")]
    assert manager._build_session.await_count == 3  # One root, two private worker contexts.
    assert a.run_dir != b.run_dir != parent.run_dir
    assert a.runtime_dir != b.runtime_dir
    assert a.workflow_run_dir == b.workflow_run_dir == parent.run_dir
    for child, other in [(a, b), (b, a)]:
        binds, _, _ = child._agent_runtime_launch_spec(runtime_type="test", uses_codex_account=False)
        mounts = dict(binds)
        assert mounts["/run"] == parent.run_dir
        assert mounts["/chats"] == child.run_dir + "/chats"
        assert mounts["/runtime"] == child.runtime_dir
        assert not any(source.startswith(other.run_dir) for source in mounts.values())
    assert (await manager.operational_snapshot())["resident"] == 1
    assert await manager.get_loaded_session("tenant", a_scope) is a
    await manager.close_session("tenant", "workflow")
    assert parent.closed and a.closed and b.closed


@pytest.mark.asyncio
async def test_child_activity_pauses_parent_ttl_until_last_worker_finishes(manager):
    child = await manager.get_session("tenant", project_workspace_scope_id("private"),
                                      user_id="alice", expose_runtime=True)
    parent = child._workflow_parent
    parent._begin_activity()  # A concurrent workflow run.
    child._begin_activity()  # Includes waiting/HITL for the entire Agent stream.
    parent.last_used = time.monotonic() - 100
    assert await manager.sweep_idle() == 0
    assert parent._inflight_operations == 2
    child._end_activity()
    assert parent._inflight_operations == 1
    assert (await manager.status("tenant", child.wf_id))["ttl_paused"]
    parent._end_activity()
    status = await manager.status("tenant", "workflow")
    assert not status["ttl_paused"]
    assert status["ttl_remaining_s"] > 9
    parent.last_used = time.monotonic() - 11
    assert await manager.sweep_idle() == 1
    await manager.drain_background_closes()
    assert parent.closed and child.closed


@pytest.mark.asyncio
async def test_closing_one_private_worker_preserves_other_worker_and_run(manager):
    a = await manager.get_session("tenant", project_workspace_scope_id("private-a"),
                                  user_id="alice", expose_runtime=True)
    b = await manager.get_session("tenant", project_workspace_scope_id("private-b"),
                                  user_id="bob", expose_runtime=True)
    parent = a._workflow_parent
    b._begin_activity()
    await manager.close_session("tenant", a.wf_id)
    assert a.closed and not b.closed and not parent.closed
    assert parent._inflight_operations == 1
    assert await manager.get_loaded_session("tenant", a.wf_id) is None
    assert await manager.get_loaded_session("tenant", b.wf_id) is b
    b._end_activity()
    await manager.close_session("tenant", "workflow")


@pytest.mark.asyncio
async def test_failed_private_persistence_retains_parent_and_shared_run(manager):
    child = await manager.get_session("tenant", project_workspace_scope_id("private"),
                                      user_id="alice", expose_runtime=True)
    parent = child._workflow_parent
    child.writeback_vfs.side_effect = OSError("object store offline")
    status = await manager.close_session("tenant", "workflow")
    assert status["status"] == "releasing"
    assert not parent.closed and not child.closed
    assert parent._chat_workspaces[child.wf_id] is child
    with pytest.raises(RuntimeError, match="sandbox_persistence_incomplete"):
        await manager.get_session("tenant", child.wf_id, user_id="alice", expose_runtime=True)
    child.writeback_vfs.side_effect = None
    await manager.sweep_idle()
    await manager.drain_background_closes()
    assert parent.closed and child.closed


@pytest.mark.asyncio
async def test_private_attachment_hot_write_never_enters_shared_run(manager):
    scope = project_workspace_scope_id("private")
    child = await manager.get_session("tenant", scope, user_id="alice", expose_runtime=True)
    assert await manager.mirror_vfs_write("tenant", scope, "/chats/chat/attachments/report.txt", b"private")
    assert (Path(child.run_dir) / "chats/chat/attachments/report.txt").read_bytes() == b"private"
    assert not (Path(child.workflow_run_dir) / "chats/chat/attachments/report.txt").exists()
    # A file-only caller must reuse, not rebuild, an existing Runtime worker.
    assert await manager.get_session("tenant", scope, user_id="alice", expose_runtime=False) is child
    assert manager._build_session.await_count == 2
    await manager.close_session("tenant", "workflow")


@pytest.mark.asyncio
async def test_account_disconnect_only_releases_that_users_runtime(manager):
    a = await manager.get_session("tenant", project_workspace_scope_id("private-a"),
                                  user_id="alice", expose_runtime=True)
    b = await manager.get_session("tenant", project_workspace_scope_id("private-b"),
                                  user_id="bob", expose_runtime=True)
    a._bound_runtime_uses_codex_account = b._bound_runtime_uses_codex_account = True
    assert await manager.invalidate_codex_account_sessions("tenant", "alice") == 1
    await manager.drain_background_closes()
    assert a.closed and not b.closed and not b._workflow_parent.closed
    await manager.close_session("tenant", "workflow")
