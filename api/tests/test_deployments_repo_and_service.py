"""Deployments T2 — DeploymentsRepo CRUD + tenant resolver.

Coverage:

* ``DeploymentsRepo.get`` returns ``None`` for soft-deleted rows.
* ``DeploymentsRepo.list_for_tenant`` filters by trigger_type / enabled
  and excludes soft-deleted rows.
* ``DeploymentsRepo.update`` patches columns; same call on a
  soft-deleted row is a silent no-op.
* ``resolve_deployment_and_bind_tenant(api_key=...)`` finds the row by
  hash and binds ``tenant_id_var``.
* ``resolve_deployment_and_bind_tenant(slug=...)`` finds the row and
  binds ``tenant_id_var``.
* ``resolve_deployment_and_bind_tenant`` returns ``None`` for an unknown
  key / slug, and for the no-args fast-fail.

Like ``test_tasks_routes_v2.py``, each test
seeds its own fixtures inline via ``pg_engine`` (superuser, RLS-bypass)
for the auth tables, then ``app_engine`` (non-superuser, RLS-bound) +
``set_config('app.tenant_id', ...)`` for the deployments rows.

Resolver tests follow the reconciler-test pattern
(``test_batch_submit_and_reconciler.py``):
``monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)`` redirects
``get_admin_engine`` to the RLS-bypassing superuser engine, so the
resolver's ``session_scope_admin`` reads the row without needing a
``set_config`` ceremony — exactly how a true production admin role
would behave.
"""
from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.services.deployments_service import (
    resolve_deployment_and_bind_tenant,
)
from vibecanvas_api.services.tenant_db import tenant_id_var
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo


# --------------------------------------------------------------------- seed


async def _seed_tenant_and_user(pg_engine, tenant_id, user_id) -> None:
    """Insert tenant + user via the RLS-bypassing superuser engine."""
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": tenant_id},
        )
        await c.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:u, :t, :e)"
            ),
            {"u": user_id, "t": tenant_id,
             "e": f"t2-{uuid.uuid4().hex[:6]}@example.com"},
        )


async def _seed_workflow(app_engine, wf_id, tenant_id, user_id) -> None:
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo

    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=wf_id,
            name="Deployment Workflow",
        )


async def _seed_api_deployment(
    app_engine, *, tenant_id, user_id, wf_id, slug, api_key_plain,
) -> uuid.UUID:
    """Insert an api-trigger deployment under ``tenant_id``."""
    dep_id = uuid.uuid4()
    h = hashlib.sha256(api_key_plain.encode()).hexdigest()
    async with session_scope(tenant_id=str(tenant_id)) as session:
        await DeploymentsRepo(session).insert(
            id=dep_id,
            tenant_id=tenant_id,
            user_id=user_id,
            owner_id=user_id,
            wf_id=wf_id,
            name="A bot",
            slug=slug,
            trigger_type="api",
            version_pin="specific",
            pinned_major=1,
            pinned_sub=0,
            api_key_hash=h,
        )
    return dep_id


async def _seed_webhook_deployment(
    app_engine, *, tenant_id, user_id, wf_id, slug,
) -> uuid.UUID:
    """Insert a webhook-trigger deployment under ``tenant_id``."""
    dep_id = uuid.uuid4()
    async with session_scope(tenant_id=str(tenant_id)) as session:
        secret_ref = await secret_service().put_text(
            session,
            tenant_id=tenant_id,
            purpose="deployment_webhook_hmac",
            resource_type="deployment",
            resource_id=dep_id,
            plaintext="whsec_test",
        )
        await DeploymentsRepo(session).insert(
            id=dep_id,
            tenant_id=tenant_id,
            user_id=user_id,
            owner_id=user_id,
            wf_id=wf_id,
            name="WH",
            slug=slug,
            trigger_type="webhook",
            version_pin="head",
            hmac_secret_ref=secret_ref,
            hmac_secret_version=1,
        )
    return dep_id


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_repo_soft_delete_excludes_from_get(pg_engine, app_engine):
    """``soft_delete`` makes ``get`` return ``None`` for the same id."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    slug = f"sd-{uuid.uuid4().hex[:6]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)
    dep_id = await _seed_api_deployment(
        app_engine, tenant_id=tenant_id, user_id=user_id, wf_id=wf_id,
        slug=slug, api_key_plain="key-soft-delete",
    )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = DeploymentsRepo(s)
        assert (await repo.get(dep_id)) is not None
        await repo.soft_delete(dep_id)
        await s.commit()
    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = DeploymentsRepo(s)
        assert (await repo.get(dep_id)) is None


@pytest.mark.asyncio
async def test_repo_list_filters_and_excludes_deleted(pg_engine, app_engine):
    """``list_for_tenant`` honours ``trigger_type`` and ``enabled``
    filters; soft-deleted rows never appear."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)
    api_id = await _seed_api_deployment(
        app_engine, tenant_id=tenant_id, user_id=user_id, wf_id=wf_id,
        slug=f"api-{uuid.uuid4().hex[:6]}", api_key_plain="k-list-1",
    )
    wh_id = await _seed_webhook_deployment(
        app_engine, tenant_id=tenant_id, user_id=user_id, wf_id=wf_id,
        slug=f"wh-{uuid.uuid4().hex[:6]}",
    )
    deleted_id = await _seed_api_deployment(
        app_engine, tenant_id=tenant_id, user_id=user_id, wf_id=wf_id,
        slug=f"del-{uuid.uuid4().hex[:6]}", api_key_plain="k-list-2",
    )
    async with session_scope(tenant_id=str(tenant_id)) as s:
        await DeploymentsRepo(s).soft_delete(deleted_id)
        await s.commit()

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = DeploymentsRepo(s)
        all_rows = await repo.list_for_tenant()
        ids = {r["id"] for r in all_rows}
        assert api_id in ids
        assert wh_id in ids
        assert deleted_id not in ids, (
            "soft-deleted row leaked into list_for_tenant"
        )

        matching = await repo.list_for_tenant(wf_id=wf_id)
        assert {row["id"] for row in matching} == {api_id, wh_id}
        assert all(row["active_version"] is None for row in matching)
        assert await repo.list_for_tenant(wf_id="nonexistent-workflow") == []
        assert (await repo.summary_for_tenant(wf_id=wf_id))["active"] == 2
        assert (await repo.summary_for_tenant(wf_id="nonexistent-workflow"))["active"] == 0
        api_only = await repo.list_for_tenant(trigger_type="api")
        assert {r["id"] for r in api_only} == {api_id}

        webhook_only = await repo.list_for_tenant(trigger_type="webhook")
        assert {r["id"] for r in webhook_only} == {wh_id}


@pytest.mark.asyncio
async def test_repo_update_patches_and_skips_deleted(pg_engine, app_engine):
    """``update`` writes named columns; same call on a soft-deleted row
    is a silent no-op (WHERE filters it)."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)
    dep_id = await _seed_api_deployment(
        app_engine, tenant_id=tenant_id, user_id=user_id, wf_id=wf_id,
        slug=f"up-{uuid.uuid4().hex[:6]}", api_key_plain="k-update",
    )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = DeploymentsRepo(s)
        await repo.update(dep_id, name="renamed", rate_limit_qps=42)
        await s.commit()
    async with session_scope(tenant_id=str(tenant_id)) as s:
        row = await DeploymentsRepo(s).get(dep_id)
        assert row["name"] == "renamed"
        assert row["rate_limit_qps"] == 42

    # Soft-delete, then update — must be a no-op.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = DeploymentsRepo(s)
        await repo.soft_delete(dep_id)
        await repo.update(dep_id, name="ghost")
        await s.commit()
    # Re-read raw via pg_engine (RLS-bypass) past the repo's
    # ``deleted_at IS NULL`` filter to confirm ``name`` is still
    # ``renamed``, not ``ghost``.
    async with pg_engine.connect() as c:
        row = (await c.execute(
            text("SELECT name FROM deployments WHERE id = :id"),
            {"id": dep_id},
        )).one()
    assert row[0] == "renamed", (
        "update() must not touch soft-deleted rows"
    )


@pytest.mark.asyncio
async def test_resolve_by_api_key_binds_tenant(
    monkeypatch, pg_engine, app_engine,
):
    """``resolve_deployment_and_bind_tenant(api_key=...)`` returns the
    row and sets ``tenant_id_var``."""
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)
    api_key_plain = f"secret-{uuid.uuid4().hex}"
    dep_id = await _seed_api_deployment(
        app_engine, tenant_id=tenant_id, user_id=user_id, wf_id=wf_id,
        slug=f"by-key-{uuid.uuid4().hex[:6]}",
        api_key_plain=api_key_plain,
    )

    tenant_id_var.set(None)
    dep = await resolve_deployment_and_bind_tenant(api_key=api_key_plain)

    assert dep is not None
    assert dep["id"] == dep_id
    assert dep["tenant_id"] == tenant_id
    assert tenant_id_var.get() == tenant_id


@pytest.mark.asyncio
async def test_resolve_invalid_key_returns_none(monkeypatch, pg_engine):
    """Unknown api_key → ``None`` (no CV mutation)."""
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    tenant_id_var.set(None)
    dep = await resolve_deployment_and_bind_tenant(api_key="does-not-exist")
    assert dep is None
    assert tenant_id_var.get() is None


@pytest.mark.asyncio
async def test_resolve_no_args_returns_none():
    """Both args None → fast-fail ``None`` (no DB roundtrip)."""
    tenant_id_var.set(None)
    dep = await resolve_deployment_and_bind_tenant()
    assert dep is None


@pytest.mark.asyncio
async def test_resolve_by_slug_binds_tenant(
    monkeypatch, pg_engine, app_engine,
):
    """``resolve_deployment_and_bind_tenant(slug=...)`` (webhook flow)
    returns the row and sets ``tenant_id_var``."""
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)
    slug = f"wh-resolve-{uuid.uuid4().hex[:6]}"
    dep_id = await _seed_webhook_deployment(
        app_engine, tenant_id=tenant_id, user_id=user_id, wf_id=wf_id,
        slug=slug,
    )

    tenant_id_var.set(None)
    dep = await resolve_deployment_and_bind_tenant(slug=slug)

    assert dep is not None
    assert dep["id"] == dep_id
    assert dep["tenant_id"] == tenant_id
    assert tenant_id_var.get() == tenant_id


@pytest.mark.asyncio
async def test_deployment_time_sorting_before_pagination(pg_engine, app_engine):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)
    ids = [await _seed_api_deployment(app_engine, tenant_id=tenant_id, user_id=user_id,
        wf_id=wf_id, slug=f"sort-{uuid.uuid4().hex[:8]}", api_key_plain=f"sort-key-{i}") for i in range(3)]
    async with session_scope(tenant_id=str(tenant_id)) as session:
        for i, dep_id in enumerate(ids):
            await session.execute(text("UPDATE deployments SET created_at='2026-01-01'::timestamptz + :n * interval '1 day' WHERE id=:id"), {"n": i, "id": dep_id})
    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = DeploymentsRepo(session)
        for direction, expected in [("asc", ids), ("desc", ids[::-1])]:
            for offset, expected_id in enumerate(expected):
                rows = await repo.list_for_tenant(sort_order=direction, offset=offset, limit=1)
                assert [row["id"] for row in rows] == [expected_id]
        # Never-invoked resources remain deterministic in both directions.
        for direction in ["asc", "desc"]:
            rows = await repo.list_for_tenant(sort_by="last_invoked_at", sort_order=direction)
            assert [row["id"] for row in rows] == sorted(ids)
