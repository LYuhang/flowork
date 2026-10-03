from __future__ import annotations

import pytest

from vibecanvas_api.services.agent_runtime.instructions import (
    command_instructions_for_modes,
)


def test_command_instructions_are_resolved_once_by_backend_policy() -> None:
    instructions = command_instructions_for_modes(
        {"browser", "workflow"},
        activated_this_turn={"workflow"},
    )

    assert [item.name for item in instructions] == ["browser", "workflow"]
    assert instructions[0].activated_this_turn is False
    assert instructions[1].activated_this_turn is True
    assert "Browser mode" in instructions[0].content
    assert "WORKFLOW mode" in instructions[1].content
    assert all(item.version == 30 for item in instructions)


def test_workflow_command_is_a_playbook_not_a_parameter_reference():
    content = command_instructions_for_modes({"workflow"})[0].content
    assert "there is no connect, status or version set" in content
    assert "flowork-cli workflow <command> --help" in content
    assert "node_schema" in content
    assert "version list" in content
    assert "Small saved changes" in content and "Large rewrites" in content
    assert "flowork-cli workflow check" in content
    assert 'render_preview(type="workflow"' in content
    assert "resubmit the entire group" in content
    assert "do not upload again" in content
    assert 'flowork-cli config get --scope model_api' in content
    assert 'flowork-cli config get --scope workflow' in content
    assert 'OpenRouter account connections' in content
    assert "--clear-tags" not in content
    assert "current_workflow_major" not in content
    assert "CONFIG_SCHEMA" not in content
    assert "Core node specs" not in content


@pytest.mark.parametrize("mode", ["workflow", "task", "deployment"])
def test_workflow_discovery_instructions_use_cli(mode):
    instruction = command_instructions_for_modes({mode})[0]
    assert ("flowork-cli workflow list" in instruction.content or "Use list to discover" in instruction.content)
    assert "list_workflows" not in instruction.content


def test_command_instruction_activation_must_be_active() -> None:
    with pytest.raises(ValueError, match="not active"):
        command_instructions_for_modes(
            {"workflow"},
            activated_this_turn={"browser"},
        )


def test_diagram_instruction_projects_exact_active_file_ref() -> None:
    file_ref = {
        "path": "/data/diagrams/system.drawio",
        "revision": "sha256:revision",
        "source_hash": "sha256:source",
    }
    active_context = {
        "file_ref": file_ref,
    }
    instruction = command_instructions_for_modes(
        {"diagram"},
        active_diagram=active_context,
    )[0]
    assert '"revision": "sha256:revision"' in instruction.content
    assert '"editable_source_path": "/data/diagrams/system.drawio"' in instruction.content
