"""Pending CLI approvals require a live command lease, including after refresh."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from vibecanvas_api.storage import hitl_repo


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [True, False])
@pytest.mark.parametrize("operation", ["workflow.delete", "task.create", "task.delete", "deployment.create", "deployment.delete", "knowledge.create", "knowledge.upload", "knowledge.delete"])
async def test_materialization_expires_only_dead_cli_approval(monkeypatch, live, operation):
    row = SimpleNamespace(private_key_id="key", tenant_id="tenant", chat_id="chat",
        hitl_request_id="hitl_1", run_id="turn", private_ciphertext="ciphertext", private_nonce="nonce",
        status="pending", is_interacted=False)
    encrypted = {"runtime_correlation_json": {"source": "flowork_cli", "runtime_request_id": "call", "runtime_method": operation}}
    monkeypatch.setattr(hitl_repo, "content_encryption_service", lambda: SimpleNamespace(decrypt_json=AsyncMock(return_value=encrypted)))
    results = [SimpleNamespace(first=lambda: (1,) if live else None), SimpleNamespace(first=lambda: ("hitl_1",))]
    session = SimpleNamespace(execute=AsyncMock(side_effect=results))
    repo = hitl_repo.HitlRepo(session)
    persist = AsyncMock()
    monkeypatch.setattr(repo, "_store_request_private", persist)
    assert await repo._materialize_request(row) is row
    assert row.status == ("pending" if live else "cancelled")
    assert session.execute.await_count == (1 if live else 2)
    expected_table = "knowledge_cli_leases" if operation.startswith("knowledge.") else ("deployment_cli_leases" if operation.startswith("deployment.") else ("task_cli_leases" if operation.startswith("task.") else "workflow_cli_leases"))
    assert expected_table in str(session.execute.await_args_list[0].args[0])
    assert persist.await_count == (0 if live else 1)
    if not live:
        assert row.is_interacted is True
        assert "no longer active" in row.decision_payload_json["reason"]


@pytest.mark.asyncio
async def test_other_approval_sources_do_not_require_a_cli_lease(monkeypatch):
    row = SimpleNamespace(private_key_id="key", tenant_id="tenant", chat_id="chat",
        hitl_request_id="hitl_1", private_ciphertext="ciphertext", private_nonce="nonce", status="pending")
    monkeypatch.setattr(hitl_repo, "content_encryption_service", lambda: SimpleNamespace(
        decrypt_json=AsyncMock(return_value={"runtime_correlation_json": {"source": "codex"}})))
    session = SimpleNamespace(execute=AsyncMock())
    assert await hitl_repo.HitlRepo(session)._materialize_request(row) is row
    session.execute.assert_not_awaited()
