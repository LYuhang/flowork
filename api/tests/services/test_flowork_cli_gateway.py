import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.flowork_cli import cli
from vibecanvas_api.services.agent_runtime.cli_gateway import (
    CliGateway,
    mark_guidance_loaded,
    needs_guidance_update,
    platform_guidance,
    prepare_platform_guidance,
)


@pytest.fixture(autouse=True)
def lifecycle_transport(monkeypatch):
    """Model the short start/poll/cancel wire protocol around business mocks."""
    activate = CliGateway.activate
    async def wrapped_activate(self, business):
        calls = {}
        async def control(operation, arguments):
            key = arguments["call_id"]
            if operation == "cli.start":
                calls[key] = asyncio.create_task(business(arguments["operation"], arguments["arguments"]))
                return {"started": True}
            if operation == "cli.cancel":
                task = calls.pop(key, None)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                return {"cancelled": True}
            task = calls[key]
            return {"sequence": 1 if task.done() else 0, "event":
                {"terminal": True, "result": task.result()} if task.done() else None}
        return await activate(self, control)
    monkeypatch.setattr(CliGateway, "activate", wrapped_activate)


async def exchange(endpoint, body):
    reader, writer = await asyncio.open_unix_connection(endpoint)
    try:
        writer.write(json.dumps(body).encode() + b"\n")
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_large_request_and_response_have_no_fixed_size_ceiling():
    workflow = {"value": "x" * (9 * 1024 * 1024)}
    reply = {"id": "wf", "version": "v1.sv0", "node_count": 0, "workflow": workflow}
    invoke = AsyncMock(return_value=reply)
    gateway = CliGateway()
    try:
        env = await gateway.activate(invoke)
        result = await asyncio.to_thread(cli.request, env["FLOWORK_CLI_SOCKET"], {"workflow": workflow}, operation="workflow.check")
        assert result == reply
        invoke.assert_awaited_once_with("workflow.check", {"workflow": workflow})
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_standalone_launcher_and_inactive_runtime():
    gateway = CliGateway()
    invoke = AsyncMock(return_value={"workflows": [], "next_offset": None})
    try:
        env = await gateway.activate(invoke)
        launcher = Path(env["FLOWORK_CLI_SOCKET"]).with_name("flowork-cli")
        process = await asyncio.create_subprocess_exec(
            str(launcher), "workflow", "list", "--limit", "3",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0
        assert json.loads(stdout) == {"workflows": [], "next_offset": None}
        assert stderr == b""
        invoke.assert_awaited_once_with("workflow.list", {"limit": 3, "offset": 0})
        await gateway.deactivate()
        result = await exchange(env["FLOWORK_CLI_SOCKET"], {"operation": "workflow.list", "arguments": {}})
        assert result["error"] == "runtime_unavailable"
    finally:
        await gateway.close()
    assert not launcher.exists()


@pytest.mark.asyncio
async def test_old_inflight_call_cannot_continue_in_new_turn():
    entered = asyncio.Event()

    async def pending(_operation, _arguments):
        entered.set()
        await asyncio.Future()

    gateway = CliGateway()
    try:
        first_env = await gateway.activate(pending)
        pending_call = asyncio.create_task(exchange(first_env["FLOWORK_CLI_SOCKET"], {"operation": "workflow.list", "arguments": {}}))
        await asyncio.wait_for(entered.wait(), timeout=5)
        await gateway.deactivate()
        assert (await pending_call)["error"] == "runtime_unavailable"
        next_invoke = AsyncMock(return_value={"workflows": [], "next_offset": None})
        second_env = await gateway.activate(next_invoke)
        assert second_env == first_env  # no forced native-thread fork each turn
        next_invoke.assert_not_awaited()
        assert await exchange(second_env["FLOWORK_CLI_SOCKET"], {"operation": "workflow.list", "arguments": {}}) == {"workflows": [], "next_offset": None}
        next_invoke.assert_awaited_once_with("workflow.list", {"limit": 20, "offset": 0})
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"operation": "workflow.delete", "arguments": {}},
    {"operation": "workflow.list", "arguments": {}, "user_id": "other"},
    {"operation": "workflow.list", "arguments": []},
])
async def test_gateway_rejects_non_allowlisted_requests(body):
    gateway = CliGateway()
    invoke = AsyncMock()
    try:
        env = await gateway.activate(invoke)
        assert (await exchange(env["FLOWORK_CLI_SOCKET"], body))["error"] == "invalid_arguments"
        invoke.assert_not_awaited()
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", [("workflow.create", {"name": "New"}), ("workflow.upload", {"workflow_id": "wf", "major": "v1", "workflow": {}}), ("workflow.version.create", {"workflow_id": "wf", "major": "v1"}), ("workflow.delete", {"workflow_id": "wf"})])
async def test_mutation_interrupted_after_dispatch_reports_unknown(operation, arguments):
    entered = asyncio.Event()

    async def pending(actual_operation, actual_arguments):
        assert actual_operation == operation
        assert all(actual_arguments[key] == value for key, value in arguments.items())
        entered.set()
        await asyncio.Future()

    gateway = CliGateway()
    try:
        env = await gateway.activate(pending)
        call = asyncio.create_task(exchange(env["FLOWORK_CLI_SOCKET"], {"operation": operation, "arguments": arguments}))
        await asyncio.wait_for(entered.wait(), timeout=5)
        await gateway.deactivate()
        assert (await call)["error"] == "result_unknown"
    finally:
        await gateway.close()


def test_guidance_is_managed_and_old_threads_receive_revision(tmp_path):
    prepare_platform_guidance(str(tmp_path))
    assert (tmp_path / "AGENTS.md").read_text() == platform_guidance()
    assert not needs_guidance_update(str(tmp_path), None)
    assert needs_guidance_update(str(tmp_path), "existing-thread")
    mark_guidance_loaded(str(tmp_path), "existing-thread")
    assert not needs_guidance_update(str(tmp_path), "existing-thread")
    assert needs_guidance_update(str(tmp_path), "other-thread")


def test_platform_guidance_is_navigation_not_a_cli_manual():
    text = platform_guidance()
    assert "Managed by Flowork, not a memory file." in text
    assert "Platform guidance revision:" not in text
    assert "browser-cli-v" not in text
    assert "flowork-cli workflow <command>" in text and "--help" in text
    assert "render_preview" in text and "run --node" in text
    assert "operation" in text and "version list / create" in text
    assert "--node_update_file" not in text and "--clear-tags" not in text
    assert "config_schema" not in text and "current_workflow_subversion" not in text


def test_platform_guidance_prose_has_no_hard_wrapping():
    for paragraph in platform_guidance().strip().split("\n\n"):
        if paragraph.startswith(("#", "|", "- ")):
            continue
        assert "\n" not in paragraph


def test_platform_guidance_explains_path_visibility_and_lifetime():
    text = platform_guidance()
    for path in ("/data", "/memory", "/logs", "/mount", "/run", "/skills"):
        assert f"| `{path}` |" in text
    assert "Agent | Workflow nodes" in text
    assert "Visible in Chat CLI runs" in text
    assert "independent Task or" in text
    assert "User-scoped persistent files" in text
    assert "Not mounted into the Agent runtime" in text
    assert "cross-sandbox synchronization" in text
    assert "platform-private runtime state" in text


def test_unmanaged_guidance_is_never_overwritten(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("User instructions", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unmanaged"):
        prepare_platform_guidance(str(tmp_path))
    assert path.read_text() == "User instructions"


def test_guidance_symlink_is_not_followed(tmp_path):
    target = tmp_path / "private.txt"
    target.write_text("private", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(target)
    with pytest.raises(RuntimeError, match="symlink"):
        prepare_platform_guidance(str(tmp_path))
    assert target.read_text() == "private"
