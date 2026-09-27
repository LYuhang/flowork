"""Choices are one waiting call, not a completed tool plus a new Agent turn."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from vibecanvas_api.services.agent_runtime.choice_wait import wait_for_choices
from vibecanvas_api.services.agent_runtime.choice_calls import result_for
from vibecanvas_api.services.platform_mcp.interactive_tools.render_choices import ChoicesInput
from vibecanvas_api.storage import hitl_repo


def test_choices_validation():
    params = ChoicesInput(title="Files", options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}])
    assert params.selected(["a"]) == ["a"]
    for invalid in ([], "a", ["a", "b"], ["a", "a"], ["missing"], [True]):
        with pytest.raises(ValueError):
            params.selected(invalid)
    assert params.model_copy(update={"multiple": True}).selected(["b", "a"]) == ["b", "a"]
    with pytest.raises(ValidationError):
        ChoicesInput(title="Files", options=[{"id": "a", "label": "A"}, {"id": "a", "label": "Duplicate"}])


@pytest.mark.asyncio
async def test_wait_does_not_return_until_user_selected(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("vibecanvas_api.services.agent_runtime.choice_wait.asyncio.sleep", sleep)
    result = {"status": "selected", "selected_ids": ["a"], "message": "The user confirmed their selection."}
    gateway = AsyncMock(side_effect=[{"pending": True}, {"pending": True}, {"result": result}, {"result": result}])
    output = await wait_for_choices(gateway, SimpleNamespace(name="interactive"), {"title": "Choose"})
    assert json.loads(output["content"][0]["text"]) == result
    calls = [item.args[3] for item in gateway.await_args_list]
    assert [item["action"] for item in calls] == ["start", "poll", "poll", "ack"]
    assert len({item["call_id"] for item in calls}) == 1
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_wait_cancellation_expires_same_call():
    started = asyncio.Event()
    calls = []
    async def gateway(operation, server, tool, arguments):
        calls.append(arguments)
        started.set()
        return {"pending": True}
    task = asyncio.create_task(wait_for_choices(gateway, None, {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [c["action"] for c in calls] == ["start", "cancel"]
    assert calls[0]["call_id"] == calls[1]["call_id"]


@pytest.mark.parametrize("live", [True, False])
@pytest.mark.asyncio
async def test_choice_lease_expiry_disables_artifact(monkeypatch, live):
    row = SimpleNamespace(private_key_id="key", tenant_id="tenant", chat_id="chat", hitl_request_id="hitl",
        run_id="turn", artifact_id="artifact", private_ciphertext="cipher", private_nonce="nonce", status="pending")
    private = {"runtime_correlation_json": {"source": "render_choices", "runtime_request_id": "hitl"}}
    monkeypatch.setattr(hitl_repo, "content_encryption_service", lambda: SimpleNamespace(decrypt_json=AsyncMock(return_value=private)))
    session = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(first=lambda: (1,) if live else None), SimpleNamespace(first=lambda: (1,))]))
    repo = hitl_repo.HitlRepo(session)
    artifact = SimpleNamespace(is_interacted=False)
    monkeypatch.setattr(repo, "get_artifact", AsyncMock(return_value=artifact))
    monkeypatch.setattr(repo, "_store_request_private", AsyncMock())
    monkeypatch.setattr(repo, "_store_artifact_private", AsyncMock())
    await repo._materialize_request(row)
    assert "interactive_call_leases" in str(session.execute.await_args_list[0].args[0])
    assert row.status == ("pending" if live else "cancelled")
    assert artifact.is_interacted == (not live)
    if not live:
        assert result_for(row)["status"] == "expired"


def test_persisted_selection_is_not_mislabeled_after_process_exit():
    row = SimpleNamespace(status="submitted", interaction_result_json={"selected_ids": ["a"]}, decision_payload_json={})
    assert result_for(row)["status"] == "selected"


@pytest.mark.asyncio
async def test_ack_failure_does_not_erase_received_choice():
    result = {"status": "selected", "selected_ids": ["a"], "message": "The user confirmed their selection."}
    gateway = AsyncMock(side_effect=[{"result": result}, RuntimeError("Disconnected during ack")])
    assert (await wait_for_choices(gateway, None, {}))["structured_content"] == result


@pytest.mark.asyncio
async def test_decision_validates_under_lock_and_ignores_forged_result(monkeypatch):
    row = SimpleNamespace(status="pending", artifact_id=None, run_id=None,
        runtime_correlation_json={"source": "render_choices"},
        agent_payload_json={"title": "Choose", "options": [{"id": "a", "label": "A"}]})
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)), flush=AsyncMock())
    repo = hitl_repo.HitlRepo(session)
    monkeypatch.setattr(repo, "_materialize_request", AsyncMock(return_value=row))
    monkeypatch.setattr(repo, "_store_request_private", AsyncMock())
    with pytest.raises(ValueError):
        await repo.resolve(hitl_request_id="id", decision="submit", decision_payload={"widget_state": {"selected_ids": ["foreign"]}})
    assert row.status == "pending"
    assert "FOR UPDATE" in str(session.execute.await_args_list[0].args[0])
    result, applied = await repo.resolve(hitl_request_id="id", decision="submit",
        decision_payload={"widget_state": {"selected_ids": ["a"]}},
        interaction_result={"status": "selected", "selected_ids": ["forged"]})
    assert applied and result.interaction_result_json == {"status": "selected", "selected_ids": ["a"]}
    _, applied = await repo.resolve(hitl_request_id="id", decision="cancel", decision_payload={})
    assert not applied and row.status == "submitted"
