from __future__ import annotations

import pytest

from vibecanvas_api.agents.tools import workspace_fs


def test_skill_mount_is_only_in_readable_roots(monkeypatch):
    monkeypatch.delenv("VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX", raising=False)
    monkeypatch.setattr(workspace_fs.os.path, "isdir", lambda _path: True)
    assert "/skills" in workspace_fs.roots(include_read_only=True)
    assert "/skills" not in workspace_fs.roots()


def test_workflow_mounts_are_supported_without_chat_flag(monkeypatch):
    monkeypatch.delenv("VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX", raising=False)
    monkeypatch.setattr(workspace_fs.os.path, "isdir", lambda _path: True)
    assert "/runs" in workspace_fs.roots()
    assert "/work" in workspace_fs.roots()
    assert "/runtime" not in workspace_fs.roots()


def test_missing_roots_fail_explicitly(monkeypatch):
    monkeypatch.setattr(workspace_fs.os.path, "isdir", lambda _path: False)
    with pytest.raises(RuntimeError, match="no mounted roots"):
        workspace_fs.roots()
