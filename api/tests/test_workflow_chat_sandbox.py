"""Canvas Chat shares Workflow files; chat directories are organization only."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.sandbox.manager import SandboxManager, SandboxSession


@pytest.mark.asyncio
async def test_workflow_and_chat_reuse_session_and_shared_run_and_chats(tmp_path):
    root = tmp_path / "workflow"
    (root / "chats" / "first").mkdir(parents=True)
    (root / "chats" / "first" / "notes.txt").write_text("shared notes")
    (root / "result.json").write_text('{"value": 42}')
    session = SandboxSession(
        tenant_id="tenant", wf_id="workflow", user_id="user", run_dir=str(root),
        runtime_dir=str(tmp_path / "runtime"), overlay_dir=None,
        provider=MagicMock(), base_binds=[], expose_run=True,
    )
    manager = SandboxManager(max_resident=1, idle_ttl_s=10)
    manager._build_session = AsyncMock(return_value=session)
    run = await manager.get_session("tenant", "workflow", user_id="user", expose_runtime=True)
    scope = project_workspace_scope_id("internal-project", workflow_id="workflow")
    chat = await manager.get_session("tenant", scope, user_id="user", expose_runtime=True)
    assert chat is run
    manager._build_session.assert_awaited_once()
    mounts, _, _ = chat._agent_runtime_launch_spec(runtime_type="test", uses_codex_account=False)
    mounts = dict(mounts)
    assert mounts["/run"] == str(root)
    assert mounts["/chats"] == str(root / "chats")
    # Creating a second conversation does not replace or narrow the mount.
    assert await manager.mirror_vfs_write("tenant", scope, "/chats/second/notes.txt", b"second notes")
    assert (root / "chats/first/notes.txt").read_text() == "shared notes"
    assert (root / "chats/second/notes.txt").read_bytes() == b"second notes"
    assert (root / "result.json").exists()
