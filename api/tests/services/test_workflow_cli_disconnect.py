"""Regression: retired binding commands and implicit targets must never dispatch."""
import pytest
from vibecanvas_api.flowork_cli.cli import CliUsageError, validate_arguments
from vibecanvas_api.services.agent_resources import authorization
from vibecanvas_api.storage.chat_repo import ChatRepo, without_workflow_state

@pytest.mark.parametrize("operation", ["connect", "disconnect", "status", "version.set"])
def test_removed_operations(operation):
    with pytest.raises(CliUsageError):
        validate_arguments("workflow." + operation, {})

@pytest.mark.parametrize("operation", ["get", "update", "download", "upload", "check", "run", "run-batch", "operation", "version.list", "version.create", "delete"])
def test_explicit_target_required(operation):
    with pytest.raises(CliUsageError):
        validate_arguments("workflow." + operation, {})

def test_retired_storage_and_authorization_helpers_are_removed():
    assert not hasattr(ChatRepo, "set_current_workflow_id")
    assert not hasattr(ChatRepo, "get_current_workflow_id")
    for method in ("_current_workflow_selection", "_save_workflow_selection", "connect_authorized_workflow", "disconnect_authorized_workflow"):
        assert not hasattr(authorization, method)

def test_cleanup_is_targeted_and_does_not_mutate_input():
    original = {"current_workflow_id": "old", "current_workflow_major": 2, "current_workflow_subversion": 9,
                "browser": {"id": "keep"}, "notes": "keep"}
    assert without_workflow_state(original) == {"browser": {"id": "keep"}, "notes": "keep"}
    assert original["current_workflow_id"] == "old"
