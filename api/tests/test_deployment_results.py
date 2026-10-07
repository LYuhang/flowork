"""Invocation observation, safe external results and current-key authorization."""

import asyncio
import hashlib
import json
import time
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.services import deployment_observer
from vibecanvas_api.services.deployment_results import external_result, sync_result_response
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


@pytest.mark.parametrize("state,code", [("succeeded", 200), ("failed", 502), ("timed_out", 504), ("cancelled", 502)])
def test_external_result_excludes_internal_outputs_and_raw_errors(state, code):
    detail = {
        "id": str(uuid.uuid4()),
        "status": state,
        "workflow": {"secret": "private"},
        "result": {
            "final_outputs": {"__start__": {"secret": "private"}, "__end__": {"answer": 42}},
            "error_dict": {"node_2": "private traceback and credentials"},
            "execution_time": 1.25,
        },
    }
    payload = external_result(detail)
    assert "private" not in json.dumps(payload)
    assert payload["outputs"] == {"answer": 42}
    assert payload["exec_time_ms"] == 1250
    response = sync_result_response(detail)
    assert (200 if isinstance(response, dict) else response.status_code) == code


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_http_wait_does_not_convert_sync_or_cancel_dispatch(pg_engine, monkeypatch, disconnect):
    from tests.storage.test_workflow_history import owner

    tenant, actor, _ = await owner()
    invocation = str(uuid.uuid4())
    async with session_scope(tenant_id=tenant) as session:
        await WorkflowHistoryRepo(session).create(
            execution_id=invocation,
            tenant_id=tenant,
            wf_id="wf-observe",
            source_type="deployment",
            source_id="test-deployment",
            initiator_user_id=actor,
            workflow={},
            inputs={},
            approvers={},
        )
    finish = asyncio.Event()

    async def dispatch():
        await finish.wait()
        async with session_scope(tenant_id=tenant) as session:
            history = WorkflowHistoryRepo(session)
            await history.bind_runtime(invocation, "generation")
            await history.persist_events(
                invocation,
                "generation",
                [
                    {
                        "invocation_id": invocation,
                        "generation": "generation",
                        "seq": 1,
                        "type": "result",
                        "status": "succeeded",
                        "final_outputs": {"__end__": {"value": 7}},
                        "error_dict": {},
                        "execution_time": 0.1,
                    }
                ],
            )

    task = deployment_observer.own_dispatch(dispatch())
    try:
        observation = asyncio.create_task(
            deployment_observer.observe_invocation(
                tenant_id=tenant,
                slug="example",
                invocation_id=invocation,
                dispatch=task,
            )
        )
        if disconnect:
            await asyncio.sleep(0.03)
            observation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observation
        else:
            await asyncio.sleep(0.15)
            assert not observation.done()
        assert not task.done()
        finish.set()
        await task
        if not disconnect:
            assert (await observation)["outputs"] == {"value": 7}
        result = await deployment_observer.observe_invocation(
            tenant_id=tenant,
            slug="example",
            invocation_id=invocation,
            dispatch=task,
        )
        assert result["outputs"] == {"value": 7}
    finally:
        finish.set()
        await task


@pytest.mark.asyncio
async def test_even_immediately_completed_approval_returns_async_ticket(pg_engine):
    from tests.storage.test_workflow_history import owner, waiting_run

    tenant, actor, _ = await owner()
    invocation, approval, event = await waiting_run(tenant, actor)
    async with session_scope(tenant_id=tenant) as session:
        history = WorkflowHistoryRepo(session)
        await history.persist_events(
            invocation,
            "generation-a",
            [
                {
                    **event,
                    "seq": 2,
                    "type": "approval_resolved",
                    "approved": True,
                    "reason": "approved",
                    "decided_at": time.time(),
                },
                {
                    **event,
                    "seq": 3,
                    "type": "result",
                    "status": "succeeded",
                    "final_outputs": {"__end__": {"approved": True}},
                    "error_dict": {},
                    "execution_time": 0.1,
                },
            ],
        )
    task = deployment_observer.own_dispatch(asyncio.sleep(0))
    await task
    response = await deployment_observer.observe_invocation(
        tenant_id=tenant,
        slug="example",
        invocation_id=invocation,
        dispatch=task,
    )
    assert response.status_code == 202
    assert json.loads(response.body)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_result_query_is_scoped_to_current_deployment_key(pg_engine, app_engine, monkeypatch):
    from tests.test_deployment_invoke_sync import _seed_full_deployment
    from vibecanvas_api.routes.deployment_invoke import get_invocation_result
    from vibecanvas_api.storage import db as db_module

    monkeypatch.setattr(db_module, "_admin_engine", pg_engine)
    tenant, slug, key, deployment = await _seed_full_deployment(pg_engine, app_engine)
    invocation = uuid.uuid4()
    async with session_scope(tenant_id=str(tenant)) as session:
        dep = (
            (await session.execute(text("SELECT * FROM deployments WHERE id=:id"), {"id": deployment})).mappings().one()
        )
        history = WorkflowHistoryRepo(session)
        await history.create(
            execution_id=str(invocation),
            tenant_id=str(tenant),
            wf_id=dep["wf_id"],
            source_type="deployment",
            source_id=str(deployment),
            initiator_user_id=str(dep["user_id"]),
            workflow={"private": "graph"},
            inputs={"private": "inputs"},
            approvers={},
        )
        await history.fail(str(invocation), error_code="execution_lost")
        await session.execute(text("UPDATE deployments SET enabled=false WHERE id=:id"), {"id": deployment})
    payload = await get_invocation_result(slug=slug, invocation_id=invocation, authorization=f"Bearer {key}")
    assert payload["status"] == "failed" and payload["error_code"] == "execution_lost"
    assert "private" not in json.dumps(payload)
    from httpx import ASGITransport, AsyncClient
    from vibecanvas_api.app import build_app

    async with AsyncClient(transport=ASGITransport(app=build_app()), base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/deployments/{slug}/runs/{invocation}", headers={"Authorization": f"Bearer {key}"}
        )
        assert response.status_code == 200
        assert response.json() == payload
    sibling_slug, sibling_key = "sibling-" + uuid.uuid4().hex, uuid.uuid4().hex
    async with session_scope(tenant_id=str(tenant)) as session:
        await session.execute(
            text("""INSERT INTO deployments
            (id,tenant_id,user_id,owner_id,wf_id,name,slug,trigger_type,version_pin,api_key_hash)
            SELECT :id,tenant_id,user_id,owner_id,wf_id,'Sibling',:slug,'api','head',:hash
            FROM deployments WHERE id=:source"""),
            {
                "id": uuid.uuid4(),
                "slug": sibling_slug,
                "hash": hashlib.sha256(sibling_key.encode()).hexdigest(),
                "source": deployment,
            },
        )
    with pytest.raises(HTTPException) as sibling:
        await get_invocation_result(slug=sibling_slug, invocation_id=invocation, authorization=f"Bearer {sibling_key}")
    assert sibling.value.status_code == 404
    _, other_slug, other_key, _ = await _seed_full_deployment(pg_engine, app_engine)
    for test_slug, authorization in [
        (slug, None),
        (slug, "Bearer wrong"),
        (other_slug, f"Bearer {other_key}"),
        (other_slug, f"Bearer {key}"),
    ]:
        with pytest.raises(HTTPException) as error:
            await get_invocation_result(slug=test_slug, invocation_id=invocation, authorization=authorization)
        assert error.value.status_code == (401 if authorization is None else 404)
    rotated = uuid.uuid4().hex
    async with session_scope(tenant_id=str(tenant)) as session:
        await session.execute(
            text("UPDATE deployments SET api_key_hash=:hash WHERE id=:id"),
            {"id": deployment, "hash": hashlib.sha256(rotated.encode()).hexdigest()},
        )
    with pytest.raises(HTTPException) as revoked:
        await get_invocation_result(slug=slug, invocation_id=invocation, authorization=f"Bearer {key}")
    assert revoked.value.status_code == 404
    assert (await get_invocation_result(slug=slug, invocation_id=invocation, authorization=f"Bearer {rotated}"))[
        "status"
    ] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,transport", [
    (429, None), (503, None), (500, None),
    (503, "sandbox_unavailable"), (503, "sandbox_deadline_exceeded"),
])
async def test_pre_dispatch_failure_has_durable_safe_result(pg_engine, app_engine, monkeypatch, code, transport):
    from unittest.mock import AsyncMock
    # This test exercises dispatch failure, not Redis admission availability.
    monkeypatch.setattr("vibecanvas_api.routes.deployment_invoke.check_rate_limit", AsyncMock())
    from tests.test_deployment_invoke_sync import _seed_full_deployment, _activate_test_revision
    from vibecanvas_api.routes import deployment_invoke
    from vibecanvas_api.storage import db as db_module

    monkeypatch.setattr(db_module, "_admin_engine", pg_engine)
    tenant, slug, key, deployment = await _seed_full_deployment(pg_engine, app_engine)
    await _activate_test_revision(tenant, deployment)

    async def unavailable(**kwargs):
        if transport:
            from vibecanvas_api.services.sandbox.service import SandboxServiceError

            raise SandboxServiceError("private credential or implementation detail", code=transport)
        raise HTTPException(code, "private credential or implementation detail")

    from vibecanvas_api.services import deployment_dispatch

    monkeypatch.setattr(deployment_dispatch, "run_workflow_sandboxed_async", unavailable)
    response = await deployment_invoke.invoke_sync(slug=slug, body={"x": 1}, authorization=f"Bearer {key}")
    assert response.status_code == code
    payload = json.loads(response.body)
    assert "private" not in response.body.decode()
    if code in {429, 503}:
        assert response.headers["Retry-After"] == "1"
    polled = await deployment_invoke.get_invocation_result(
        slug=slug,
        invocation_id=uuid.UUID(payload["invocation_id"]),
        authorization=f"Bearer {key}",
    )
    assert polled == payload


def test_approval_timeout_has_its_own_external_error_code():
    detail = {"id": str(uuid.uuid4()), "status": "timed_out", "error_code": "approval_timeout",
              "result": {"final_outputs": {}, "error_dict": {"node_2": "private details"}}}
    response = sync_result_response(detail)
    assert response.status_code == 504
    payload = json.loads(response.body)
    assert payload["status"] == "timed_out" and payload["error_code"] == "approval_timeout"
    assert "private details" not in response.body.decode()


@pytest.mark.asyncio
async def test_http_redis_outage_rejects_without_admitting_execution(pg_engine, app_engine, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from vibecanvas_api.app import build_app
    from vibecanvas_api.services import rate_limit
    from vibecanvas_api.storage import db as db_module
    from tests.test_deployment_invoke_sync import _seed_full_deployment

    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)
    tenant, slug, key, deployment = await _seed_full_deployment(pg_engine, app_engine)
    async with session_scope(tenant_id=str(tenant)) as session:
        await session.execute(text('UPDATE deployments SET rate_limit_qps=10 WHERE id=:id'), {'id': deployment})
    monkeypatch.setattr(rate_limit, '_get_redis', lambda: None)
    async with AsyncClient(transport=ASGITransport(app=build_app()), base_url='http://testserver') as client:
        response = await client.post(f'/api/v1/deployments/{slug}/invoke',
                                     headers={'Authorization': f'Bearer {key}'}, json={'x': 1})
    assert response.status_code == 503, response.text
    assert response.headers['Retry-After'] == '1'
    assert response.json()['detail'] == 'rate_limit_unavailable'
    async with session_scope(tenant_id=str(tenant)) as session:
        count = await session.scalar(text('SELECT count(*) FROM deployment_invocations WHERE deployment_id=:id'), {'id': deployment})
        assert count == 0
