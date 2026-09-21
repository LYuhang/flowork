"""Deployments T7 — POST /deployments/{slug}/runs (async submit).

Coverage:

* Happy path: 202-ish return shape — handler yields an opaque
  ``{"task_id": ...}`` invocation id and does not create a Task row.
* Missing bearer → 401.
* Wrong api_key → 404.
* Wrong URL slug (key matches deployment A, slug is unrelated) → 404.
* Router is mounted in ``build_app()``.

Strategy matches T6 (``test_deployment_invoke_sync.py``): seed tenant +
user via the superuser ``pg_engine`` (auth tables aren't RLS-scoped),
then seed workflow + workflow_versions + deployments via ``app_engine``
(RLS-bound) with an explicit ``set_config('app.tenant_id', ...)``.
``_admin_engine`` is monkeypatched onto ``pg_engine`` so
``resolve_deployment_and_bind_tenant``'s ``session_scope_admin`` can
actually find the row.

DBOS ``send_task`` is stubbed: T9 ships the worker body; here we
only assert the API-side row insert + task_id return. We call the
route handler directly (no HTTPX) so we don't have to wire the JWT
auth dependency — the deployment flow authenticates by Bearer api_key,
not by JWT.
"""
from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


# Minimal workflow content — only needs to validate as JSON for the
# ``workflow_versions.workflow`` JSONB column. The async-submit path
# does NOT load or run it; only T9's worker will. node_name uses the
# engine-required ``__start__``/``__end__`` so a later end-to-end
# test that DOES execute would still pass against the same row.
_MINIMAL_WORKFLOW = {
    "node_1": {
        "node_id": "node_1",
        "node_type": "StartNode",
        "node_name": "__start__",
        "node_description": "",
        "input_fields": {
            "x": {"type": "int", "value": 0, "reference": ""},
        },
        "output_fields": {
            "x": {"type": "int", "description": ""},
        },
        "node_config": {},
        "children": ["node_2"],
        "__attributes__": {"x": 0, "y": 0},
    },
    "node_2": {
        "node_id": "node_2",
        "node_type": "EndNode",
        "node_name": "__end__",
        "node_description": "",
        "input_fields": {
            "y": {"type": "int", "value": 0, "reference": "__start__.x"},
        },
        "output_fields": {},
        "node_config": {},
        "children": [],
        "__attributes__": {"x": 200, "y": 0},
    },
    "__meta__": {
        "workflow_name": "min",
        "workflow_description": "",
    },
}


# --------------------------------------------------------------------- seed


async def _seed_deployment(pg_engine, app_engine):
    """Seed tenant + user + workflow + workflow_versions + deployment.

    Returns ``(tenant_id, slug, api_key_plaintext, dep_id)`` — the
    plaintext key is what the caller sends in the Bearer header; its
    SHA-256 hash is what we INSERT into ``api_key_hash``.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    api_key = f"vc_t7_{uuid.uuid4().hex[:12]}"
    h = hashlib.sha256(api_key.encode()).hexdigest()
    slug = f"async-{uuid.uuid4().hex[:6]}"
    dep_id = uuid.uuid4()

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
            {
                "u": user_id, "t": tenant_id,
                "e": f"t7-{uuid.uuid4().hex[:6]}@example.com",
            },
        )

    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=wf_id,
            name="Deployment Workflow",
            initial_workflow=_MINIMAL_WORKFLOW,
        )
        await session.execute(
            text(
                "INSERT INTO deployments("
                "id, tenant_id, user_id, owner_id, wf_id, name, slug, "
                "trigger_type, version_pin, pinned_major, pinned_sub, "
                "api_key_hash"
                ") VALUES ("
                ":id, :t, :u, :u, :w, 'Async', :s, "
                "'api', 'specific', 1, 0, :h"
                ")"
            ),
            {
                "id": dep_id, "t": tenant_id, "u": user_id, "w": wf_id,
                "s": slug, "h": h,
            },
        )
    return tenant_id, slug, api_key, dep_id


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_runs_returns_202_without_task_row(
    pg_engine, app_engine, monkeypatch,
):
    """Async submit returns an opaque invocation id without creating a Task row."""
    from vibecanvas_api.routes.deployment_invoke import invoke_async
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    # Stub DBOS send_task — T9 ships the worker; here we only assert
    # the API-side row insert. ``send_task`` runs through
    # ``asyncio.to_thread`` inside ``DeploymentsService.submit``, so the
    # stub must be a plain (sync) callable.
    from vibecanvas_api.services import deployments_service
    enqueue = AsyncMock()
    monkeypatch.setattr(
        deployments_service, "enqueue_background_job_in_transaction", enqueue,
    )

    tenant_id, slug, api_key, _ = await _seed_deployment(pg_engine, app_engine)
    result = await invoke_async(
        slug=slug, body={"x": 21},
        authorization=f"Bearer {api_key}",
    )
    assert "task_id" in result
    task_id = result["task_id"]

    # Deployment invocations are not Task Center rows.
    async with app_engine.connect() as c:
        await c.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(tenant_id)},
        )
        row = (await c.execute(
            text("SELECT id FROM tasks WHERE id = :id"),
            {"id": task_id},
        )).first()
    assert row is None
    assert enqueue.await_args.kwargs["kwargs"] == {"invocation_id": task_id}

    async with pg_engine.connect() as c:
        invocation = (
            await c.execute(
                text(
                    "SELECT private_ciphertext, private_nonce, private_key_id "
                    "FROM deployment_invocations WHERE id = :id"
                ),
                {"id": task_id},
            )
        ).one()
    assert all(invocation)
    assert "21" not in invocation.private_ciphertext


@pytest.mark.asyncio
async def test_runs_rejects_missing_bearer(
    pg_engine, app_engine, monkeypatch,
):
    """No ``Authorization`` header → 401 (RFC compliance)."""
    from vibecanvas_api.routes.deployment_invoke import invoke_async
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, _, _ = await _seed_deployment(pg_engine, app_engine)
    with pytest.raises(HTTPException) as exc:
        await invoke_async(slug=slug, body={}, authorization=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_runs_rejects_bad_key(pg_engine, app_engine, monkeypatch):
    """Unknown api_key → 404 (uniform existence-leak guard)."""
    from vibecanvas_api.routes.deployment_invoke import invoke_async
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, _, _ = await _seed_deployment(pg_engine, app_engine)
    with pytest.raises(HTTPException) as exc:
        await invoke_async(
            slug=slug, body={}, authorization="Bearer vc_wrong",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_runs_rejects_wrong_slug(pg_engine, app_engine, monkeypatch):
    """Key matches deployment A, slug is unrelated → 404. A key holder
    for A cannot drive B's slug."""
    from vibecanvas_api.routes.deployment_invoke import invoke_async
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, _, api_key, _ = await _seed_deployment(pg_engine, app_engine)
    with pytest.raises(HTTPException) as exc:
        await invoke_async(
            slug="not-here", body={},
            authorization=f"Bearer {api_key}",
        )
    assert exc.value.status_code == 404


def test_runs_route_mounted():
    """``/{slug}/runs`` is wired into ``build_app()`` (defence-in-depth
    against a stale import that compiles but never mounts)."""
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts
    app = build_app()
    paths = {r.path for r in application_route_contexts(app)}
    assert any("/deployments/{slug}/runs" in p for p in paths), (
        f"runs route missing; got {sorted(paths)}"
    )
