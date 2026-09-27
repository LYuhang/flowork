from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.agent_runtime import browser_transfer_calls as transfers
from vibecanvas_api.services.agent_runtime.browser_download_choices import DownloadScope, DownloadCandidate, SelectionRequired


CAP = SimpleNamespace(tenant_id="tenant", user_id="user", chat_id="chat", turn_id="turn", runtime_session_id="runtime", approval_mode="always_ask")
SCOPE = DownloadScope("transport", "browser", 1)
INPUT = {"choice_set_id": "registered", "candidate_id": "candidate"}
CANDIDATE = DownloadCandidate(status="ready", candidate_id="candidate", name="file.bin", bytes=7, source="https://example.com")


def row(status="approved"):
    return SimpleNamespace(chat_id="chat", run_id="turn", status=status, artifact_id=None,
        runtime_correlation_json={"source": "browser_transfer"},
        resume_payload_json={"user_id": "user", "runtime_session_id": "runtime", "input": INPUT,
            "scope": asdict(SCOPE), "capture_id": "capture", "tab_id": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "denied", "cancelled", "submitted"])
async def test_each_chunk_requires_actual_approval(monkeypatch, status):
    monkeypatch.setattr(transfers, "resolve_context", AsyncMock())
    monkeypatch.setattr(transfers, "HitlRepo", lambda session: SimpleNamespace(get_request=AsyncMock(return_value=row(status))))
    session = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(PermissionError, match="not approved"):
        await transfers.authorize_native_read(session, CAP, transfer_id="id", capture_id="capture")
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_expired_command_cannot_reuse_a_previously_approved_transfer(monkeypatch):
    monkeypatch.setattr(transfers, "resolve_context", AsyncMock())
    monkeypatch.setattr(transfers, "HitlRepo", lambda session: SimpleNamespace(get_request=AsyncMock(return_value=row())))
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)))
    with pytest.raises(PermissionError, match="command ended"):
        await transfers.authorize_native_read(session, CAP, transfer_id="id", capture_id="capture")
    assert "cancel_requested_at IS NULL" in str(session.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_read_grant_is_exactly_the_registered_file_and_original_fence(monkeypatch):
    monkeypatch.setattr(transfers, "resolve_context", AsyncMock())
    monkeypatch.setattr(transfers, "HitlRepo", lambda session: SimpleNamespace(get_request=AsyncMock(return_value=row())))
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (1,))))
    preflight = AsyncMock(return_value=(SCOPE, {"tab_id": 1}, CANDIDATE))
    monkeypatch.setattr(transfers, "_preflight", preflight)
    assert await transfers.authorize_native_read(session, CAP, transfer_id="id", capture_id="capture") == {
        "tab_id": 1, "capture_id": "capture", "candidate_id": "candidate"}
    preflight.return_value = (DownloadScope("transport", "browser", 2), {"tab_id": 1}, CANDIDATE)
    with pytest.raises(PermissionError, match="ownership changed"):
        await transfers.authorize_native_read(session, CAP, transfer_id="id", capture_id="capture")


@pytest.mark.parametrize("field,value", [("user_id", "foreign"), ("runtime_session_id", "new-runtime")])
def test_identity_dimensions_cannot_be_rebound(field, value):
    request = row()
    request.resume_payload_json[field] = value
    with pytest.raises(PermissionError):
        transfers._owned(request, CAP)


@pytest.mark.asyncio
async def test_always_allow_does_not_bypass_user_selection(monkeypatch):
    session = SimpleNamespace(execute=AsyncMock())
    @asynccontextmanager
    async def scope(**kwargs):
        yield session
    cap = SimpleNamespace(**{**CAP.__dict__, "approval_mode": "always_allow"})
    monkeypatch.setattr(transfers, "verify_agent_capability", lambda *args, **kwargs: cap)
    monkeypatch.setattr(transfers, "resolve_context", AsyncMock())
    monkeypatch.setattr(transfers, "session_scope", scope)
    repo = SimpleNamespace(get_request=AsyncMock(return_value=None), create_request=AsyncMock())
    monkeypatch.setattr(transfers, "HitlRepo", lambda session: repo)
    monkeypatch.setattr(transfers, "_preflight", AsyncMock(side_effect=SelectionRequired("User selection is required.")))
    response = await transfers.dispatch("token", {"action": "start", "call_id": "call", "input": INPUT})
    assert response["status"] == "selection_required"
    repo.create_request.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_prints_decision_and_releases_transfer_on_command_end():
    from vibecanvas_api.services.agent_runtime.browser_cli_runtime import BrowserCliRuntime
    transfer = AsyncMock(side_effect=[{"pending": True, "status": "awaiting_approval"},
        {"status": "denied", "message": "User denied the transfer."}, {"status": "finished"}])
    runtime = BrowserCliRuntime(AsyncMock(), transfer=transfer)
    emit = AsyncMock()
    result = await runtime._approve_transfer(INPUT, emit)
    assert result["status"] == "denied"
    assert runtime._transfer_calls == set()
    assert emit.await_args.args[0]["_progress"]["message"] == "User denied the transfer."
    await runtime._finish_transfers()
    calls = [c.args[0] for c in transfer.await_args_list]
    assert [c["action"] for c in calls] == ["start", "poll", "finish"]
    assert len({c["call_id"] for c in calls}) == 1
    assert runtime._transfer_calls == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["always_allow", "always_ask", "agent"])
@pytest.mark.parametrize("direction", ["download", "upload"])
async def test_start_creates_lease_and_uses_existing_approval_artifact(monkeypatch, mode, direction):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (1,))))

    @asynccontextmanager
    async def scope(**kwargs):
        yield session

    cap = SimpleNamespace(**{**CAP.__dict__, "approval_mode": mode})
    monkeypatch.setattr(transfers, "verify_agent_capability", lambda *args, **kwargs: cap)
    monkeypatch.setattr(transfers, "resolve_context", AsyncMock())
    monkeypatch.setattr(transfers, "session_scope", scope)
    upload = direction == "upload"
    input = {"direction": "upload", "tab_id": "tab_test", "destination": "https://example.com/upload",
             "files": [{"name": "a.txt", "bytes": 7, "sha256": "a" * 64}]} if upload else INPUT
    monkeypatch.setattr(transfers, "_preflight", AsyncMock(return_value=(SCOPE,
        {"tab_id": "tab_test" if upload else 1, "capture_id": None if upload else "capture"}, None if upload else CANDIDATE)))
    request = row("pending")

    async def create(**kwargs):
        request.resume_payload_json = kwargs["resume_payload_json"]
        request.runtime_correlation_json = kwargs["runtime_correlation_json"]
        return request

    async def resolve(**kwargs):
        request.status = "approved"
        return request, True

    repo = SimpleNamespace(get_request=AsyncMock(return_value=None), create_request=AsyncMock(side_effect=create),
        create_interactive_artifact=AsyncMock(), link_artifact_hitl=AsyncMock(), resolve=AsyncMock(side_effect=resolve))
    monkeypatch.setattr(transfers, "HitlRepo", lambda session: repo)
    response = await transfers.dispatch("token", {"action": "start", "call_id": "call", "input": input})
    assert any("INSERT INTO interactive_call_leases" in str(c.args[0]) for c in session.execute.await_args_list)
    assert request.resume_payload_json["capture_id"] == (None if upload else "capture")
    assert request.resume_payload_json["input"] == input
    assert request.runtime_correlation_json["runtime_method"] == "browser." + direction
    if upload:
        prompt = repo.create_request.await_args.kwargs["prompt_text"]
        assert "a.txt" in prompt and "7 bytes" in prompt and "https://example.com/upload" in prompt
    if mode == "always_allow":
        assert response["status"] == "approved"
        assert response["automatic"] is True
        repo.create_interactive_artifact.assert_not_called()
        repo.resolve.assert_awaited_once()
    else:
        assert response["status"] == "awaiting_approval"
        assert response["pending"] is True
        repo.create_interactive_artifact.assert_awaited_once()
        repo.link_artifact_hitl.assert_awaited_once()
        repo.resolve.assert_not_called()
        assert [e["event_type"] for e in response["_events"]] == ["HITL_REQUIRED", "CHAT_EVENT", "CHAT_EVENT"]
        started = response["_events"][1]["payload"]
        assert isinstance(started["message_id"], str)
        assert isinstance(started["arguments"], str)
        assert started["tool_call_id"] == response["_events"][2]["payload"]["tool_call_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "denied"])
async def test_waiting_for_second_transfer_keeps_prior_grant_live(monkeypatch, decision):
    from vibecanvas_api.services.agent_runtime import browser_cli_runtime as runtime_module

    now = 0
    expires_at = 60

    async def tick(_):
        nonlocal now
        now += 1

    async def transfer(message):
        nonlocal expires_at
        if message["action"] == "renew":
            assert message["call_id"] == "earlier-approved-call"
            if now >= expires_at:
                return {"status": "expired"}
            expires_at = now + 60
            return {"status": "approved"}
        if message["action"] == "finish":
            return {"status": "finished"}
        return {"pending": True, "status": "awaiting_approval"} if now < 75 else {"status": decision}

    monkeypatch.setattr(runtime_module.asyncio, "sleep", tick)
    runtime = runtime_module.BrowserCliRuntime(AsyncMock(), transfer=transfer)
    runtime._transfer_calls.add("earlier-approved-call")
    result = await runtime._approve_transfer(INPUT, AsyncMock())
    assert result["status"] == decision
    assert now == 75  # Virtual seconds: human wait exceeds the real 60s lease.
    assert (await transfer({"action": "renew", "call_id": "earlier-approved-call"}))["status"] == "approved"


@pytest.mark.asyncio
async def test_prior_grant_revocation_stops_waiting_for_another_approval(monkeypatch):
    from vibecanvas_api.services.agent_runtime import browser_cli_runtime as runtime_module

    polls = 0

    async def transfer(message):
        nonlocal polls
        if message["action"] == "renew":
            return {"status": "expired"}
        polls += 1
        assert polls <= 2, "Revocation must stop the pending approval loop"
        return {"pending": True, "status": "awaiting_approval"}

    monkeypatch.setattr(runtime_module.asyncio, "sleep", AsyncMock())
    runtime = runtime_module.BrowserCliRuntime(AsyncMock(), transfer=transfer)
    runtime._transfer_calls.add("revoked-approved-call")
    with pytest.raises(PermissionError, match="permission is no longer active"):
        await runtime._approve_transfer(INPUT, AsyncMock())


@pytest.mark.asyncio
async def test_upload_preflight_requires_live_browser_scope_not_download_selection(monkeypatch):
    live = AsyncMock(return_value=SCOPE)
    monkeypatch.setattr(transfers, "current_scope", live)
    selection = AsyncMock(side_effect=AssertionError("Uploads have no native download candidate"))
    monkeypatch.setattr(transfers, "require_selection", selection)
    input = transfers._input({"direction": "upload", "tab_id": "tab_test", "destination": "https://example.com",
        "files": [{"name": "a", "bytes": 0, "sha256": "a" * 64}]})
    assert await transfers._preflight("session", CAP, input) == (SCOPE, {"tab_id": "tab_test", "capture_id": None}, None)
    live.assert_awaited_once_with("session", CAP)
    live.side_effect = PermissionError("Browser changed")
    with pytest.raises(PermissionError):
        await transfers._preflight("session", CAP, input)


@pytest.mark.parametrize("change", [{"files": []}, {"tab_id": "10"}, {"files": [{"name": "a", "bytes": -1, "sha256": "a" * 64}]},
                                   {"files": [{"name": "a", "bytes": 1, "sha256": "wrong"}]}])
def test_upload_manifest_rejects_incomplete_identity_or_file_metadata(change):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        transfers._input({"direction": "upload", "tab_id": "tab_test", "destination": "https://example.com",
            "files": [{"name": "a", "bytes": 0, "sha256": "a" * 64}], **change})


@pytest.mark.asyncio
async def test_renewal_after_completed_capture_checks_fence_not_retired_candidates(monkeypatch):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (1,))))

    @asynccontextmanager
    async def scope(**kwargs):
        yield session

    monkeypatch.setattr(transfers, "verify_agent_capability", lambda *args, **kwargs: CAP)
    monkeypatch.setattr(transfers, "resolve_context", AsyncMock())
    monkeypatch.setattr(transfers, "session_scope", scope)
    monkeypatch.setattr(transfers, "HitlRepo", lambda session: SimpleNamespace(get_request=AsyncMock(return_value=row())))
    monkeypatch.setattr(transfers, "_preflight", AsyncMock(side_effect=AssertionError("Completed capture no longer exists")))
    fence = AsyncMock(return_value=SCOPE)
    monkeypatch.setattr(transfers, "current_scope", fence)
    assert (await transfers.dispatch("token", {"action": "renew", "call_id": "call"}))["status"] == "approved"
    fence.return_value = DownloadScope("transport", "browser", 2)
    with pytest.raises(PermissionError, match="ownership changed"):
        await transfers.dispatch("token", {"action": "renew", "call_id": "call"})
