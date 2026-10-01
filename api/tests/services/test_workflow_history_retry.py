"""A failed commit must not advance the event watermark or release the runtime."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from vibecanvas_api.services.sandbox import workflow_execution_driver as driver


def database_error(code):
    cause = RuntimeError("database failure")
    cause.sqlstate = code
    return DBAPIError("statement", {}, cause)


@pytest.mark.asyncio
async def test_retry_after_uncertain_commit_reuses_batch_before_releasing_runtime(monkeypatch):
    attempts = 0
    frames = [{"seq": 1, "type": "result"}]

    @asynccontextmanager
    async def scope():
        nonlocal attempts
        attempts += 1
        yield None
        if attempts == 1:
            raise database_error("40001")

    repo = SimpleNamespace(
        persist_events=AsyncMock(return_value=1),
        get=AsyncMock(return_value={"status": "succeeded"}),
        pending_commands=AsyncMock(return_value=[]),
        detail=AsyncMock(return_value={"result": {"final_outputs": {"answer": 42}}}),
    )
    monkeypatch.setattr(driver, "WorkflowHistoryRepo", lambda session: repo)
    slot = SimpleNamespace(alive=True, release=AsyncMock())
    owner = driver.WorkflowExecutionDriver(
        tenant_id="tenant", execution_id="execution", slot=slot, persist_artifacts=AsyncMock(),
    )
    monkeypatch.setattr(owner, "_session", scope)
    through, _, _, result = await owner._persist_frames("generation", frames)
    assert attempts == 2
    assert through == 1 and result["final_outputs"] == {"answer": 42}
    assert repo.persist_events.await_count == 2
    assert all(call.args == ("execution", "generation", frames) for call in repo.persist_events.await_args_list)
    slot.release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,budget", [("23505", 10), ("40001", 0)])
async def test_permanent_or_exhausted_database_failure_is_not_retried(monkeypatch, code, budget):
    attempts = 0

    @asynccontextmanager
    async def scope():
        nonlocal attempts
        attempts += 1
        raise database_error(code)
        yield  # pragma: no cover

    owner = driver.WorkflowExecutionDriver(
        tenant_id="tenant", execution_id="execution",
        slot=SimpleNamespace(alive=True), persist_artifacts=AsyncMock(),
    )
    monkeypatch.setattr(owner, "_session", scope)
    monkeypatch.setattr(driver, "PERSISTENCE_RETRY_SECONDS", budget)
    with pytest.raises(DBAPIError):
        await owner._persist_frames("generation", [])
    assert attempts == 1
