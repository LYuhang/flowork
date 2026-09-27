"""Candidate IDs are references, not user consent or file-transfer permission."""
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.agent_runtime import browser_download_choices as choices
from vibecanvas_api.services.platform_mcp.interactive_tools.render_choices import ChoicesRequest


CAP = SimpleNamespace(tenant_id="tenant", organization_id="tenant", user_id="user", chat_id="chat",
                      turn_id="turn", runtime_session_id="runtime", session_id="session")
SCOPE = choices.DownloadScope("transport", "browser", 1)


def candidate(id="a"):
    return choices.DownloadCandidate(status="ready", candidate_id=id, name=f"report-{id}.csv", bytes=3, source="https://example.com")


def test_choice_request_requires_one_source():
    assert ChoicesRequest(title="Download", choice_set_id="downloads_1").options == []
    assert ChoicesRequest(title="Other", options=[{"id": "a", "label": "A"}]).choice_set_id == ""
    for payload in ({}, {"options": []}, {"choice_set_id": " "},
                    {"choice_set_id": "set", "options": [{"id": "a", "label": "Forged"}]}):
        with pytest.raises(ValueError):
            ChoicesRequest(title="Choose", **payload)


@pytest.mark.asyncio
async def test_file_labels_are_from_registered_metadata(monkeypatch):
    monkeypatch.setattr(choices, "load_candidates", AsyncMock(return_value=({"hitl_request_id": None}, [candidate(), candidate("b")])))
    request = ChoicesRequest(title="Download", choice_set_id="set")
    result = await choices.resolve_choices(None, CAP, request, "hitl")
    assert result.options[0].label == "report-a.csv"
    assert result.options[0].description == "3 bytes · https://example.com"
    with pytest.raises(ValueError, match="exactly one"):
        await choices.resolve_choices(None, CAP, request.model_copy(update={"multiple": True}), "hitl")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,selected", [("pending", ["a"]), ("cancelled", ["a"]),
    ("expired", ["a"]), ("submitted", ["b"]), ("submitted", ["a", "b"])])
async def test_unconfirmed_or_different_file_cannot_transfer(monkeypatch, status, selected):
    monkeypatch.setattr(choices, "load_candidates", AsyncMock(return_value=({"hitl_request_id": "hitl"}, [candidate(), candidate("b")])))
    row = SimpleNamespace(status=status, chat_id="chat", run_id="turn",
        runtime_correlation_json={"source": "render_choices"}, interaction_result_json={"selected_ids": selected})
    monkeypatch.setattr(choices, "HitlRepo", lambda session: SimpleNamespace(get_request=AsyncMock(return_value=row)))
    with pytest.raises(PermissionError, match="selection is required"):
        await choices.require_selection(None, CAP, "set", "a")


@pytest.mark.asyncio
async def test_only_original_user_selection_counts(monkeypatch):
    record = {"hitl_request_id": "hitl"}
    monkeypatch.setattr(choices, "load_candidates", AsyncMock(return_value=(record, [candidate(), candidate("b")])))
    row = SimpleNamespace(status="submitted", chat_id="chat", run_id="turn",
        runtime_correlation_json={"source": "render_choices"}, interaction_result_json={"selected_ids": ["a"]})
    monkeypatch.setattr(choices, "HitlRepo", lambda session: SimpleNamespace(get_request=AsyncMock(return_value=row)))
    assert (await choices.require_selection(None, CAP, "set", "a"))[1].candidate_id == "a"
    row.run_id = "another-turn"
    with pytest.raises(PermissionError):
        await choices.require_selection(None, CAP, "set", "a")
    with pytest.raises(PermissionError, match="not a registered"):
        await choices.require_selection(None, CAP, "set", "guessed")


@pytest.mark.asyncio
async def test_single_candidate_requires_registration_but_not_choice(monkeypatch):
    monkeypatch.setattr(choices, "load_candidates", AsyncMock(return_value=({"hitl_request_id": None}, [candidate()])))
    assert (await choices.require_selection(None, CAP, "set", "a"))[1].name == "report-a.csv"
    # This function returns metadata only, never a read grant/approval.


@pytest.mark.asyncio
async def test_load_fences_all_identity_dimensions_before_decryption(monkeypatch):
    monkeypatch.setattr(choices, "current_scope", AsyncMock(return_value=SCOPE))
    execute = AsyncMock(return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: None)))
    decrypt = AsyncMock()
    monkeypatch.setattr(choices, "content_encryption_service", lambda: SimpleNamespace(decrypt_json=decrypt))
    with pytest.raises(PermissionError):
        await choices.load_candidates(SimpleNamespace(execute=execute), CAP, "guessed")
    sql, params = execute.await_args.args
    for field in ("tenant_id", "user_id", "chat_id", "run_id", "runtime_session_id", "transport_id", "browser_session_id", "browser_generation", "revoked_at"):
        assert field in str(sql)
    assert params["generation"] == 1 and params["run"] == "turn"
    decrypt.assert_not_called()


@pytest.mark.asyncio
async def test_registration_rechecks_fence_and_refuses_partial_metadata(monkeypatch):
    monkeypatch.setattr(choices, "current_scope", AsyncMock(return_value=SCOPE))
    session = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(PermissionError):
        await choices.register_candidates(session, CAP, choices.DownloadScope("other", "browser", 1),
            tab_id=1, capture_id="capture", candidates=[candidate().model_dump()])
    for candidates in ([], [{"status": "downloading"}], [candidate().model_dump(), candidate().model_dump()]):
        with pytest.raises(ValueError):
            await choices.register_candidates(session, CAP, SCOPE, tab_id=1, capture_id="capture", candidates=candidates)
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_registration_encrypts_metadata_and_invalidates_older_snapshot(monkeypatch):
    monkeypatch.setattr(choices, "current_scope", AsyncMock(return_value=SCOPE))
    execute = AsyncMock(return_value=SimpleNamespace(first=lambda: None))
    encrypt = AsyncMock(return_value=SimpleNamespace(ciphertext="encrypted", nonce="nonce", key_id="key"))
    monkeypatch.setattr(choices, "content_encryption_service", lambda: SimpleNamespace(encrypt_json=encrypt))
    set_id = await choices.register_candidates(SimpleNamespace(execute=execute), CAP, SCOPE,
        tab_id=1, capture_id="capture", candidates=[candidate().model_dump()])
    assert set_id.startswith("downloads_")
    assert encrypt.await_args.kwargs["value"]["candidates"][0]["name"] == "report-a.csv"
    assert any("SET revoked_at=now()" in str(call.args[0]) for call in execute.await_args_list)
    assert "report-a.csv" not in str(execute.await_args_list)
    assert execute.await_args.args[1]["cipher"] == "encrypted"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,transport,error", [
    ("lost", "transport", choices.BrowserTransportUnavailable),
    ("attached", None, choices.BrowserTransportUnavailable),
    ("released", "transport", PermissionError),
])
async def test_reconnect_is_not_permission_to_read_or_permanent_choice_expiry(monkeypatch, status, transport, error):
    monkeypatch.setattr(choices, "resolve_context", AsyncMock())
    monkeypatch.setattr(choices.registry, "find_for_session", lambda *args: transport)
    monkeypatch.setattr(choices, "ChatRepo", lambda *args: SimpleNamespace(get_browser_binding=AsyncMock(return_value={
        "status": status, "browser_session_id": "browser", "browser_session_generation": 1})))
    with pytest.raises(error):
        await choices.current_scope(None, CAP)


@pytest.mark.asyncio
async def test_choice_poll_during_reconnect_preserves_pending_without_renewing_lease(monkeypatch):
    from vibecanvas_api.services.agent_runtime import choice_calls
    session = SimpleNamespace(execute=AsyncMock())
    @asynccontextmanager
    async def scope(**kwargs):
        yield session
    row = SimpleNamespace(status="pending", chat_id=CAP.chat_id, run_id=CAP.turn_id,
                          runtime_correlation_json={"choice_set_id": "registered"})
    repo = SimpleNamespace(get_request=AsyncMock(return_value=row), resolve=AsyncMock())
    monkeypatch.setattr(choice_calls, "session_scope", scope)
    monkeypatch.setattr(choice_calls, "verify_agent_capability", lambda *args, **kwargs: CAP)
    monkeypatch.setattr(choice_calls.invocation, "_require_tool_capability", lambda *args, **kwargs: None)
    monkeypatch.setattr(choice_calls.agent_context, "resolve_context", AsyncMock())
    monkeypatch.setattr(choice_calls, "HitlRepo", lambda session: repo)
    monkeypatch.setattr(choices, "load_candidates", AsyncMock(side_effect=choices.BrowserTransportUnavailable()))
    assert await choice_calls.dispatch("token", {"action": "poll", "call_id": "call"}) == {"pending": True, "_events": []}
    repo.resolve.assert_not_called()
    session.execute.assert_not_called()
    # Same fence restored: the SAME call resumes heartbeating, not a new card.
    monkeypatch.setattr(choices, "load_candidates", AsyncMock(return_value=({}, [candidate()])))
    assert (await choice_calls.dispatch("token", {"action": "poll", "call_id": "call"}))["pending"]
    assert "UPDATE interactive_call_leases" in str(session.execute.await_args.args[0])
