import base64
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from vibecanvas_api.flowork_cli import cli, deployment_cli
from vibecanvas_api.services.agent_runtime.cli_deployments import (
    needs_approval,
    settings,
)


def test_simplified_list(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "request", lambda endpoint, args, **kw: seen.append((args, kw)) or {"deployments": []})
    assert cli.main(["deployment", "list"], socket_path="test") == 0
    assert seen[0] == ({"limit": 20, "offset": 0}, {"operation": "deployment.list"})
    assert json.loads(capsys.readouterr().out) == {"deployments": [], "command_status": "succeeded", "event": "result"}
    assert cli.main(["deployment", "list", "--status", "enabled"], socket_path="test") != 0


def test_create_credential_private_and_never_stdout(tmp_path, monkeypatch, capsys):
    dep_id = str(uuid4())
    secret = tmp_path / "private.json"
    def request(endpoint, args, **kw):
        assert secret.exists() and secret.stat().st_mode & 0o777 == 0o600
        assert args == {"workflow_id": "wf", "name": "Demo", "trigger_type": "api", "version": "v2.sv0",
                        "slug": "demo", "mount": False, "enabled": True, "rate_limit_qps": 10}
        return {"deployment_id": dep_id, "_credential": {"api_key": "one-time-secret"}}
    monkeypatch.setattr(cli, "request", request)
    command = ["deployment", "create", "--workflow_id", "wf", "--name", "Demo", "--trigger_type", "api",
               "--version", "v2.sv0", "--secret_file", str(secret)]
    assert cli.main(command, socket_path="test") == 0
    output = capsys.readouterr().out
    assert "one-time-secret" not in output and "_credential" not in json.loads(output)
    assert json.loads(output)["credential_saved"] is True
    assert "JSON, not a raw token" in json.loads(output)["hint"]
    assert "only api_key as the Bearer token" in json.loads(output)["hint"]
    assert json.loads(secret.read_text()) == {"api_key": "one-time-secret"}
    assert cli.main(command, socket_path="test") != 0
    assert json.loads(secret.read_text())["api_key"] == "one-time-secret"


@pytest.mark.parametrize("action", ["create", "rotate_key", "status"])
def test_credential_format_is_discoverable_without_reading_secrets(action, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args(["deployment", action, "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "JSON, not a raw token" in help_text
    assert "only api_key as the Bearer token" in help_text
    assert "hmac_secret" in help_text
    assert "do not wrap it in an inputs property" in help_text
    assert "outputs" in help_text


def test_symlink_rejected_before_dispatch(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setattr(cli, "request", lambda *a, **kw: pytest.fail("must not dispatch"))
    assert cli.main(["deployment", "rotate_key", "--deployment_id", str(uuid4()),
                     "--secret_file", str(link)], socket_path="test") != 0
    assert target.read_text() == "preserve"


def test_replaced_secret_path_reports_committed_failure(tmp_path, monkeypatch, capsys):
    target = tmp_path / "secret"
    dep_id = str(uuid4())
    def request(*a, **kw):
        target.unlink()
        target.write_text("another file")
        return {"deployment_id": dep_id, "_credential": {"api_key": "private"}}
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["deployment", "rotate_key", "--deployment_id", dep_id,
                     "--secret_file", str(target)], socket_path="test") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "credential_write_failed"
    assert result["deployment_id"] == dep_id and result["credential_saved"] is False
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("operation,args", [
    ("list", {"q": "x"}), ("list", {"limit": 201}), ("list", {"offset": -1}),
    ("status", {"deployment_id": "not-id"}),
    ("update", {"deployment_id": str(uuid4())}),
    ("update", {"deployment_id": str(uuid4()), "mount": "true"}),
    ("create", {"workflow_id": "wf", "name": "x", "trigger_type": "api"}),
    ("create", {"workflow_id": "wf", "name": "x", "trigger_type": "api", "version": "v0.sv0"}),
    ("run", {"deployment_id": str(uuid4()), "inputs": []}),
    ("history", {"deployment_id": str(uuid4()), "execution_id": str(uuid4()), "limit": 20}),
    ("history", {"deployment_id": str(uuid4()), "after": "bad-cursor"}),
    ("history", {"deployment_id": str(uuid4()), "from_time": "2026-01-01"}),
])
def test_reject_bad_contract(operation, args):
    with pytest.raises(ValueError):
        deployment_cli.validate("deployment." + operation, args)


def test_cursor_and_settings():
    cursor = base64.urlsafe_b64encode(json.dumps({"id": str(uuid4()), "submitted_at": "2026-01-01T00:00:00Z"}).encode()).decode()
    assert deployment_cli.validate("deployment.history", {"deployment_id": str(uuid4()), "after": cursor})["after"] == cursor
    assert settings({"version": "v2.sv0", "mount": False}) == {"version_pin": "specific", "pinned_major": 2, "pinned_sub": 0, "mount_enabled": False}
    assert settings({"version": "v3.sv4"}) == {"version_pin": "specific", "pinned_major": 3, "pinned_sub": 4}


@pytest.mark.parametrize("operation,args,expected", [
    ("create", {}, True), ("delete", {}, True), ("run", {}, True), ("enable", {}, True),
    ("disable", {}, False), ("rotate_key", {}, True),
    ("update", {"name": "renamed"}, False), ("update", {"mount": False}, False),
    ("update", {"mount": True}, True), ("update", {"version": "v2.sv0"}, True),
    ("update", {"rate_limit_qps": 5}, False), ("update", {"rate_limit_qps": 0}, True),
    ("update", {"rate_limit_qps": 11}, True),
])
def test_agent_approval_policy(operation, args, expected):
    assert needs_approval("deployment." + operation, args, {"rate_limit_qps": 10, "mount_enabled": False}) is expected


@pytest.mark.parametrize("terminal_status", ["failed", "timed_out", "cancelled"])
def test_run_inputs_file_and_failed_exit(tmp_path, monkeypatch, capsys, terminal_status):
    inputs = tmp_path / "inputs.json"
    inputs.write_text('{"value":7}')
    def request(endpoint, args, **kw):
        assert args["inputs"] == {"value": 7}
        return {"status": terminal_status, "execution_id": "execution", "errors": {"node": "failed"}}
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["deployment", "run", "--deployment_id", str(uuid4()),
                     "--inputs_file", str(inputs)], socket_path="test") == 1
    assert json.loads(capsys.readouterr().out)["execution_id"] == "execution"


def test_run_inputs_file_preserves_json_types(tmp_path, monkeypatch, capsys):
    payload = {"text": "hello", "count": 2, "enabled": False, "items": [1, None], "object": {"x": "y"}}
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(payload))
    request = Mock(return_value={"status": "succeeded", "outputs": payload})
    monkeypatch.setattr(cli, "request", request)
    target = str(uuid4())
    assert cli.main(["deployment", "run", "--deployment_id", target,
        "--inputs_file", str(inputs)], socket_path="test") == 0
    assert request.call_args.args[1] == {"deployment_id": target, "inputs": payload}
    assert json.loads(capsys.readouterr().out)["outputs"] == payload


@pytest.mark.parametrize("content", ["[]", "null", "not json"])
def test_run_rejects_invalid_file_before_dispatch(tmp_path, monkeypatch, content):
    inputs = tmp_path / "inputs.json"
    inputs.write_text(content)
    monkeypatch.setattr(cli, "request", lambda *a, **kw: pytest.fail("must not execute"))
    assert cli.main(["deployment", "run", "--deployment_id", str(uuid4()),
        "--inputs_file", str(inputs)], socket_path="test") != 0


def test_run_file_and_inline_inputs_are_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "request", lambda *a, **kw: pytest.fail("must not execute"))
    assert cli.main(["deployment", "run", "--deployment_id", str(uuid4()),
        "--inputs", "{}", "--inputs_file", str(tmp_path / "unused.json")], socket_path="test") != 0


def test_history_export_new_directory(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "diagnostics"
    def request(endpoint, args, **kw):
        assert args["export"] is True
        return {"files": {"status.json": "{}", "metrics.json": "{}", "invocations.jsonl": ""}, "next_cursor": "next"}
    monkeypatch.setattr(cli, "request", request)
    command = ["deployment", "history", "--deployment_id", str(uuid4()), "--output_dir", str(folder)]
    assert cli.main(command, socket_path="test") == 0
    assert set(os.listdir(folder)) == {"status.json", "metrics.json", "invocations.jsonl"}
    assert folder.stat().st_mode & 0o777 == 0o700
    assert json.loads(capsys.readouterr().out)["next_cursor"] == "next"
    assert cli.main(command, socket_path="test") == 2


def test_node_errors_omit_runtime_internals():
    from vibecanvas_api.services.agent_runtime.cli_deployments import execution_errors
    assert execution_errors({"node_2": {"error_message": "Code timed out after 60s", "kwargs": {"private": "value"}},
                             "__top__": "Engine unavailable"}) == {
        "node_2": "Code timed out after 60s", "__top__": "Engine unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("generation,expires,success", [(1, 300, True), (2, 300, False), (1, -1, False)])
async def test_step_up_uses_live_server_session(monkeypatch, generation, expires, success):
    from fastapi import HTTPException
    from vibecanvas_api.auth.deps import AuthContext
    from vibecanvas_api.services.agent_runtime import cli_deployments as host
    tenant_id, user_id, session_id = str(uuid4()), str(uuid4()), str(uuid4())
    auth = AuthContext(user_id=user_id, tenant_id=tenant_id, email="", session_id=session_id,
                       authentication_strength="password", session_generation=1)
    row = SimpleNamespace(expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires),
        generation=generation, active_organization_id=tenant_id, authentication_strength="webauthn",
        step_up_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300), audience="web")
    monkeypatch.setattr(host.config, "high_risk_step_up_required", True)
    monkeypatch.setattr(host, "AuthRepo", lambda session: SimpleNamespace(get_session_by_id=AsyncMock(return_value=row)))
    params = {"ctx": auth, "session": object()}
    if success:
        await host.step_up(params)
        assert params["ctx"].authentication_strength == "webauthn"
    else:
        with pytest.raises(HTTPException) as exc:
            await host.step_up(params)
        assert exc.value.detail["code"] == "step_up_required"


@pytest.mark.parametrize("state", ["waiting_approval", "running", "succeeded"])
def test_async_run_is_reported_as_accepted_even_if_approval_already_finished(state):
    from fastapi.responses import JSONResponse
    from vibecanvas_api.services.agent_runtime.cli_deployments import invocation_result
    response = JSONResponse(status_code=202, content={"status": state, "execution_id": "original"})
    result = invocation_result("deployment", response)
    assert result["http_status"] == 202
    assert "accepted" in result["message"]
    assert "succeeded" not in result["message"]
    assert "--execution_id original" in result["hint"]


@pytest.mark.parametrize("state", ["waiting_approval", "timed_out"])
def test_history_accepts_new_execution_statuses(monkeypatch, capsys, state):
    request = Mock(return_value={"items": []})
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["deployment", "history", "--deployment_id", str(uuid4()), "--status", state], socket_path="test") == 0
    assert request.call_args.args[1]["status"] == state


def test_deployment_rejects_floating_major():
    with pytest.raises(ValueError, match='fixed --version'):
        deployment_cli.validate('deployment.update', {'deployment_id': str(uuid4()), 'major': 'v1'})
