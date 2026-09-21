"""Deployments T11 — rate limit + concurrency + counter flush (mocked Redis)."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text


# ----- check_rate_limit -----

@pytest.mark.asyncio
async def test_check_rate_limit_429_on_overflow(monkeypatch):
    from vibecanvas_api.services import rate_limit
    # Each call returns 1,2,3 — when qps=2, the 3rd call exceeds.
    fake = AsyncMock()
    fake.eval = AsyncMock(side_effect=[1, 2, 3])
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: fake)
    dep = {"id": uuid.uuid4(), "rate_limit_qps": 2}
    await rate_limit.check_rate_limit(dep)
    await rate_limit.check_rate_limit(dep)
    with pytest.raises(HTTPException) as exc:
        await rate_limit.check_rate_limit(dep)
    assert exc.value.status_code == 429
    assert exc.value.headers.get("Retry-After") == "1"


@pytest.mark.asyncio
async def test_check_rate_limit_skips_if_qps_zero(monkeypatch):
    from vibecanvas_api.services import rate_limit
    fake = AsyncMock()
    fake.eval = AsyncMock()
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: fake)
    await rate_limit.check_rate_limit({"id": "x", "rate_limit_qps": 0})
    fake.eval.assert_not_called()


@pytest.mark.asyncio
async def test_check_rate_limit_skips_if_redis_down(monkeypatch):
    from vibecanvas_api.services import rate_limit
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: None)
    # Must not raise.
    await rate_limit.check_rate_limit({"id": "x", "rate_limit_qps": 10})


@pytest.mark.asyncio
async def test_check_rate_limit_swallows_redis_error(monkeypatch):
    from vibecanvas_api.services import rate_limit
    fake = AsyncMock()
    fake.eval = AsyncMock(side_effect=ConnectionError("boom"))
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: fake)
    # Must not raise.
    await rate_limit.check_rate_limit({"id": "x", "rate_limit_qps": 10})


# ----- bump_redis_invoke_counter -----

@pytest.mark.asyncio
async def test_bump_counter_calls_incr(monkeypatch):
    from vibecanvas_api.services import rate_limit
    fake = AsyncMock()
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: fake)
    dep_id = uuid.uuid4()
    await rate_limit.bump_redis_invoke_counter(dep_id)
    fake.incr.assert_awaited_once_with(f"dep:{dep_id}:count")


# ----- flush_invoke_counters -----

@pytest.mark.asyncio
async def test_flush_no_redis_no_op(monkeypatch):
    from vibecanvas_api.background_tasks import invoke_counter_flush
    monkeypatch.setattr(invoke_counter_flush, "_get_redis", lambda: None)
    await invoke_counter_flush._flush()  # must not raise


@pytest.mark.asyncio
async def test_flush_writes_postgres(pg_engine, app_engine, monkeypatch, pg_url):
    """Seed a deployment + a Redis counter; flush; verify deployments.invoke_count."""
    from vibecanvas_api.background_tasks import invoke_counter_flush
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    # Seed.
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    dep_id = uuid.uuid4()
    async with pg_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
                        {"t": tenant_id})
        await c.execute(text(
            "INSERT INTO users(user_id, tenant_id, email) VALUES (:u, :t, :e)"
        ), {"u": user_id, "t": tenant_id, "e": f"f-{uuid.uuid4().hex[:6]}@example.com"})
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo

    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=wf_id,
            name="Flush Workflow",
        )
        await session.execute(text(
            "INSERT INTO deployments(id, tenant_id, user_id, owner_id, wf_id, name, slug, "
            "trigger_type, version_pin, pinned_major, pinned_sub, api_key_hash, invoke_count) "
            "VALUES (:id, :t, :u, :u, :w, 'D', :s, 'api', 'specific', 1, 0, 'h', 0)"
        ), {"id": dep_id, "t": tenant_id, "u": user_id, "w": wf_id,
             "s": f"f-{uuid.uuid4().hex[:6]}"})

    # Fake Redis returns one matching key with value=5.
    fake = AsyncMock()
    fake.keys = AsyncMock(return_value=[f"dep:{dep_id}:count".encode()])
    fake.getdel = AsyncMock(return_value=b"5")
    monkeypatch.setattr(invoke_counter_flush, "_get_redis", lambda: fake)

    await invoke_counter_flush._flush()
    fake.keys.assert_awaited()
    fake.getdel.assert_awaited()

    async with pg_engine.connect() as c:
        row = (await c.execute(text(
            "SELECT invoke_count FROM deployments WHERE id = :id"
        ), {"id": dep_id})).one()
    assert row.invoke_count == 5


# ----- check_tenant_concurrency -----

@pytest.mark.asyncio
async def test_concurrency_under_cap_passes(monkeypatch):
    from vibecanvas_api.services import rate_limit
    fake = AsyncMock()
    fake.get = AsyncMock(return_value=b"5")  # cached cap=5
    fake.incr = AsyncMock(return_value=3)
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: fake)
    await rate_limit.check_tenant_concurrency(uuid.uuid4(), increment=True)  # no raise


@pytest.mark.asyncio
async def test_concurrency_over_cap_429_and_decrements(monkeypatch):
    from vibecanvas_api.services import rate_limit
    fake = AsyncMock()
    fake.get = AsyncMock(return_value=b"3")  # cached cap=3
    fake.incr = AsyncMock(return_value=4)  # over cap
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: fake)
    with pytest.raises(HTTPException) as exc:
        await rate_limit.check_tenant_concurrency(uuid.uuid4(), increment=True)
    assert exc.value.status_code == 429
    fake.decr.assert_awaited()


@pytest.mark.asyncio
async def test_concurrency_cap_none_means_unlimited(monkeypatch):
    from vibecanvas_api.services import rate_limit
    fake = AsyncMock()
    fake.get = AsyncMock(return_value=b"NONE")
    monkeypatch.setattr(rate_limit, "_get_redis", lambda: fake)
    await rate_limit.check_tenant_concurrency(uuid.uuid4(), increment=True)  # no raise
    fake.incr.assert_not_called()


# ----- durable schedule registration -----

def test_flush_task_registered():
    from vibecanvas_api.background_workflows import SCHEDULE_WORKFLOWS
    assert "deployments.flush_invoke_counters" in SCHEDULE_WORKFLOWS


def test_reconciler_task_registered():
    from vibecanvas_api.background_workflows import SCHEDULE_WORKFLOWS
    assert "deployments.concurrency_reconciler" in SCHEDULE_WORKFLOWS
