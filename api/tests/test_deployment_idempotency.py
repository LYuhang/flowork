"""Real database transactions for request retry and admission rollback."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from tests.test_deployment_invoke_sync import _activate_test_revision, _seed_full_deployment
from vibecanvas_api.routes import deployment_invoke as routes
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


def payload(response):
    return response if isinstance(response, dict) else json.loads(response.body)


async def setup_deployment(pg_engine, app_engine, monkeypatch):
    import vibecanvas_api.storage.db as db

    monkeypatch.setattr(db, "_admin_engine", pg_engine)
    tenant, slug, key, dep_id = await _seed_full_deployment(pg_engine, app_engine)
    await _activate_test_revision(tenant, dep_id)
    limiter = AsyncMock()
    monkeypatch.setattr(routes, "check_rate_limit", limiter)
    monkeypatch.setattr(routes, "bump_redis_invoke_counter", AsyncMock())
    return tenant, slug, key, dep_id, limiter


@pytest.mark.asyncio
async def test_retry_reuses_live_and_finished_invocation_and_rejects_changed_input(pg_engine, app_engine, monkeypatch):
    from vibecanvas_api.services import deployment_observer

    tenant, slug, key, dep_id, limiter = await setup_deployment(pg_engine, app_engine, monkeypatch)
    monkeypatch.setattr(deployment_observer, "SYNC_WAIT_SECONDS", 0.01)
    finish = asyncio.Event()
    finished = asyncio.Event()
    dispatched = []

    async def dispatch(**kwargs):
        dispatched.append(kwargs["invocation_id"])
        try:
            await finish.wait()
            async with session_scope(tenant_id=str(tenant)) as session:
                repo = WorkflowHistoryRepo(session)
                invocation = kwargs["invocation_id"]
                await repo.bind_runtime(invocation, "generation")
                await repo.persist_events(
                    invocation,
                    "generation",
                    [
                        {
                            "seq": 1,
                            "generation": "generation",
                            "invocation_id": invocation,
                            "type": "result",
                            "status": "succeeded",
                            "final_outputs": {"__end__": {"y": 7}},
                            "error_dict": {},
                            "execution_time": 0.1,
                        }
                    ],
                )
        finally:
            finished.set()

    monkeypatch.setattr(routes, "_dispatch_invocation", dispatch)
    args = {"slug": slug, "body": {"x": 7}, "authorization": f"Bearer {key}", "idempotency_key": "client-operation"}
    try:
        # Two modest simultaneous requests exercise the database unique-key race.
        responses = await asyncio.gather(routes.invoke_sync(**args), routes.invoke_sync(**args))
        first, second = map(payload, responses)
        assert first["invocation_id"] == second["invocation_id"]
        assert all(response.status_code == 202 for response in responses)
        assert dispatched == [first["invocation_id"]]
        limiter.assert_awaited_once()
        with pytest.raises(HTTPException) as conflict:
            await routes.invoke_sync(**{**args, "body": {"x": 8}})
        assert conflict.value.status_code == 409
        # Switching to the explicit async endpoint also reuses the original admission.
        again = await routes.invoke_async(**args)
        assert payload(again)["invocation_id"] == first["invocation_id"]
        assert len(dispatched) == 1
        finish.set()
        await asyncio.wait_for(finished.wait(), 5)
        completed = await routes.invoke_sync(**args)
        assert payload(completed)["status"] == "succeeded"
        assert payload(completed)["outputs"] == {"y": 7}
        assert len(dispatched) == 1
        async with session_scope(tenant_id=str(tenant)) as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM deployment_idempotency_receipts WHERE deployment_id=:id"), {"id": dep_id}
                )
                == 1
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM deployment_invocations WHERE deployment_id=:id"), {"id": dep_id}
                )
                == 1
            )
    finally:
        finish.set()
        if dispatched:
            await asyncio.wait_for(finished.wait(), 5)


@pytest.mark.asyncio
async def test_rejected_admission_rolls_back_key_and_async_retries_enqueue_once(pg_engine, app_engine, monkeypatch):
    tenant, slug, key, dep_id, limiter = await setup_deployment(pg_engine, app_engine, monkeypatch)
    enqueue = AsyncMock()
    monkeypatch.setattr("vibecanvas_api.services.deployments_service.enqueue_background_job_in_transaction", enqueue)
    args = {
        "slug": slug,
        "body": {"x": 7},
        "authorization": f"Bearer {key}",
        "idempotency_key": "retry-after-rejection",
    }
    limiter.side_effect = HTTPException(429, "limited")
    with pytest.raises(HTTPException) as rejected:
        await routes.invoke_async(**args)
    assert rejected.value.status_code == 429
    async with session_scope(tenant_id=str(tenant)) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM deployment_idempotency_receipts WHERE deployment_id=:id"), {"id": dep_id}
            )
            == 0
        )
    limiter.side_effect = None
    responses = await asyncio.gather(routes.invoke_async(**args), routes.invoke_async(**args))
    assert payload(responses[0])["invocation_id"] == payload(responses[1])["invocation_id"]
    enqueue.assert_awaited_once()
    assert limiter.await_count == 2  # rejected request, then one successful admission


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "with space", "a" * 257])
async def test_invalid_keys_do_not_admit(pg_engine, app_engine, monkeypatch, key):
    _, slug, api_key, _, limiter = await setup_deployment(pg_engine, app_engine, monkeypatch)
    with pytest.raises(HTTPException) as invalid:
        await routes.invoke_sync(slug=slug, body={"x": 1}, authorization=f"Bearer {api_key}", idempotency_key=key)
    assert invalid.value.status_code == 422
    limiter.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_history_and_receipt(pg_engine, app_engine, monkeypatch):
    tenant, slug, key, dep_id, _ = await setup_deployment(pg_engine, app_engine, monkeypatch)
    enqueue = AsyncMock(side_effect=RuntimeError("queue unavailable"))
    monkeypatch.setattr("vibecanvas_api.services.deployments_service.enqueue_background_job_in_transaction", enqueue)
    args = {"slug": slug, "body": {"x": 7}, "authorization": f"Bearer {key}", "idempotency_key": "retry-queue"}
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await routes.invoke_async(**args)
    async with session_scope(tenant_id=str(tenant)) as session:
        for table in ("deployment_idempotency_receipts", "deployment_invocations"):
            assert (
                await session.scalar(text(f"SELECT count(*) FROM {table} WHERE deployment_id=:id"), {"id": dep_id}) == 0
            )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM workflow_execution_runs WHERE source_id=:id"), {"id": str(dep_id)}
            )
            == 0
        )
    enqueue.side_effect = None
    response = await routes.invoke_async(**args)
    assert response.status_code == 202
    assert enqueue.await_count == 2


@pytest.mark.asyncio
async def test_same_key_is_independent_for_other_deployments_and_tenants(pg_engine, app_engine, monkeypatch):
    first = await setup_deployment(pg_engine, app_engine, monkeypatch)
    second = await setup_deployment(pg_engine, app_engine, monkeypatch)
    enqueue = AsyncMock()
    monkeypatch.setattr("vibecanvas_api.services.deployments_service.enqueue_background_job_in_transaction", enqueue)
    ids = []
    for tenant, slug, key, dep_id, _ in (first, second):
        response = await routes.invoke_async(
            slug=slug, body={"x": 1}, authorization=f"Bearer {key}", idempotency_key="shared-client-key"
        )
        ids.append(payload(response)["invocation_id"])
        async with session_scope(tenant_id=str(tenant)) as session:
            # The non-superuser connection is tenant scoped: other receipts are invisible.
            visible = (
                (await session.execute(text("SELECT deployment_id FROM deployment_idempotency_receipts")))
                .scalars()
                .all()
            )
            assert visible == [dep_id]
    assert ids[0] != ids[1]
    assert enqueue.await_count == 2
    with pytest.raises(HTTPException) as unauthorized:
        await routes.invoke_async(
            slug=first[1], body={"x": 1}, authorization=f"Bearer {second[2]}", idempotency_key="shared-client-key"
        )
    assert unauthorized.value.status_code == 404
    assert enqueue.await_count == 2
