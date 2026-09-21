"""Snapshot test: pin the public surface of vibecanvas_api.

Catches regressions where someone removes/renames an export.
"""

from __future__ import annotations

import vibecanvas_api


def test_version_present():
    assert isinstance(vibecanvas_api.__version__, str)
    assert vibecanvas_api.__version__


def test_storage_exports():
    # WorkspaceStore, AsyncWriter, and StoragePaths were the
    # file-backed storage layer; deleted with the file-storage backend
    # (zero live callers). The Postgres-backed Repos remain public.
    from vibecanvas_api import (
        WorkflowRepo, ChatRepo, ExecutionRepo,
    )
    assert callable(WorkflowRepo)
    assert callable(ChatRepo)
    assert callable(ExecutionRepo)


def test_retired_manager_exports_stay_absent():
    # WorkflowVersionTree and
    # SessionIndex (managers/session_index) were legacy Gradio-era dead
    # code over the deleted file stores; deleted in T12. TemplateManager was
    # retired with the template product surface; Workflow sharing remains.
    # The file-backed task manager and local workers were deleted
    # — batch execution now flows through DBOS + the Postgres tasks
    # table (routes/tasks.py), not a public manager class.
    assert not hasattr(vibecanvas_api, "TemplateManager")


def test_config_exports():
    # The old task-manager tuning config was deleted with
    # the legacy task manager; remaining scope classes stay public.
    from vibecanvas_api import (
        config, AgentConfig, StorageConfig,
    )
    assert config is not None
    assert callable(AgentConfig)
    assert callable(StorageConfig)


def test_context_export():
    from vibecanvas_api import init_stores
    assert callable(init_stores)
