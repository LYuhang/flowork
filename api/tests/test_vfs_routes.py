from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from vibecanvas_api.routes import vfs
from vibecanvas_api.services.chat_workspace import (
    project_id_from_workspace_scope,
    project_workspace_scope_id,
)


class _WorkflowRepoStub:
    def __init__(self, _session, _user_id):
        pass

    async def get_meta(self, wf_id: str):
        if wf_id == "wf_ok":
            return {"wf_id": wf_id, "major_version": 1, "minor_version": 0}
        return None


def test_chat_workspace_scope_roundtrips_without_embedding_owner():
    scope = project_workspace_scope_id("chat/含空格/abc")
    assert scope.startswith("__projectws_v1_")
    assert project_id_from_workspace_scope(scope) == "chat/含空格/abc"
    assert "owner" not in scope


@pytest.mark.asyncio
async def test_writable_vfs_scope_accepts_only_canonical_chat_data(monkeypatch):
    monkeypatch.setattr(vfs, "WorkflowRepo", _WorkflowRepoStub)
    user_id = "user-12345678901234567890"
    scope = project_workspace_scope_id("chatabc")
    await vfs._ensure_writable_vfs_scope(
        session=object(),
        user_id=user_id,
        wf_id=scope,
        path="/data/result.txt",
    )
    with pytest.raises(HTTPException, match="invalid_folder"):
        await vfs._ensure_writable_vfs_scope(
            session=object(),
            user_id=user_id,
            wf_id=scope,
            path="/memory/private.txt",
        )


def test_retired_user_prefixed_chat_scope_is_not_decoded():
    assert project_id_from_workspace_scope(
        "__chatws_deadbeefdeadbeefdead_chatabc"
    ) is None


@pytest.mark.asyncio
async def test_chat_workspace_list_reconciles_loaded_sandbox_without_cold_start(
    monkeypatch,
):
    scope = project_workspace_scope_id("chat-live")
    loaded = AsyncMock()
    manager = AsyncMock()
    manager.get_loaded_session.return_value = loaded
    monkeypatch.setattr(vfs, "get_sandbox_manager", lambda: manager)

    assert await vfs._reconcile_loaded_chat_workspace(
        tenant_id="tenant-1",
        scope_id=scope,
    )
    manager.get_loaded_session.assert_awaited_once_with("tenant-1", scope)
    loaded.writeback_vfs.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_non_chat_vfs_list_never_reconciles_or_allocates_sandbox(monkeypatch):
    manager = AsyncMock()
    monkeypatch.setattr(vfs, "get_sandbox_manager", lambda: manager)

    assert not await vfs._reconcile_loaded_chat_workspace(
        tenant_id="tenant-1",
        scope_id="workflow-1",
    )
    manager.get_loaded_session.assert_not_awaited()
