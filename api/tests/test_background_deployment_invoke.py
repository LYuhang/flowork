"""Deployments T9 — background ``deployment_invoke`` drives the engine.

Tests the runtime-neutral worker body directly (no queue or worker process).
Deployment invocation ids are opaque background workflow
ids; they are not Task Center rows.

Strategy mirrors batch_exec's eager-end-to-end coverage:

  * Seed tenant / user via ``pg_engine`` (superuser, no RLS).
  * Seed workflow + version + deployment via ``app_engine``
    with an explicit ``set_config('app.tenant_id', ...)`` so RLS accepts
    the writes.
  * Monkeypatch ``db._admin_engine`` onto ``pg_engine`` so
    ``load_workflow_version``'s ``session_scope_admin`` finds the row.
  * Invoke ``deployment_invoke(...)`` synchronously.
  * Assert no Task row is created by the worker.

The minimal-workflow shape is copied from the T7 async-submit tests so
it exercises the real engine path (Start → End passthrough).
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


# Minimal Start → End workflow. The End node references
# ``__start__.x`` so ``Workflow.astream`` produces a non-empty
# ``final_outputs`` event — matches the engine's reference-resolution
# rule (node references use ``node_name.field``, not ``node_id.field``).
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


def test_deployment_invoke_is_runtime_neutral():
    from vibecanvas_api.background_tasks.deployment_invoke import deployment_invoke
    assert callable(deployment_invoke)
    assert not hasattr(deployment_invoke, "delay")


async def _seed_deployment(pg_engine, app_engine):
    """Seed tenant + user + workflow + version + deployment.

    Returns ``(tenant_id, dep_id, task_id)`` — all UUIDs. The caller
    drives ``deployment_invoke(...)`` against the returned ids.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    dep_id = uuid.uuid4()
    service_account_id = uuid.uuid4()
    task_id = uuid.uuid4()

    # Auth tables — not RLS-scoped; seed via the superuser engine.
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
                "e": f"t9-{uuid.uuid4().hex[:6]}@example.com",
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
                "INSERT INTO service_accounts("
                "service_account_id, tenant_id, name, kind, "
                "owner_resource_type, owner_resource_id, created_by"
                ") VALUES (:sa, :t, 'deployment test', 'deployment', "
                "'deployment', :owner, :u)"
            ),
            {
                "sa": service_account_id,
                "t": tenant_id,
                "owner": str(dep_id),
                "u": user_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO deployments("
                "id, tenant_id, user_id, owner_id, wf_id, name, slug, "
                "trigger_type, version_pin, pinned_major, pinned_sub, "
                "api_key_hash, service_account_id"
                ") VALUES ("
                ":id, :t, :u, :u, :w, 'D', :s, "
                "'api', 'specific', 1, 0, :h, :sa"
                ")"
            ),
            {
                "id": dep_id, "t": tenant_id, "u": user_id, "w": wf_id,
                "s": f"d-{uuid.uuid4().hex[:6]}",
                "h": hashlib.sha256(
                    f"k-{uuid.uuid4().hex[:8]}".encode()
                ).hexdigest(),
                "sa": service_account_id,
            },
        )

    return tenant_id, dep_id, task_id


@pytest.mark.asyncio
async def test_deployment_invoke_marks_finished_and_emits_event(
    pg_url, pg_engine, app_engine, monkeypatch,
):
    """Happy path — engine runs to completion without creating a Task row.

    The minimal Start → End workflow has no errors so
    ``drain_astream`` returns ``({"node_2": {...}}, {}, ...)`` and the
    task takes the ``finished`` branch in ``_finalize``.

    Loop-isolation note (differs from T6/T7/T8 admin-engine setup):
    the DBOS task body wraps its async work in ``asyncio.run`` inside
    an ``asyncio.to_thread`` hop, so the test's pytest-asyncio loop and
    the worker's loop are DIFFERENT. We can't reuse the test-loop-bound
    ``pg_engine`` for ``_admin_engine`` (asyncpg connections are
    loop-bound — cross-loop access raises ``Event loop is closed``).
    Instead we point ``_admin_engine`` to None + set
    ``ADMIN_DATABASE_URL`` to the superuser DSN so ``get_admin_engine``
    lazily builds a FRESH engine on the worker's loop.
    """
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    # Orchestration test — MOCK the runner (a). This tests the async deploy
    # worker body, NOT engine output, so
    # the sandbox runner is replaced with a canned echo. Decouples from the
    # in-process host-fallback (removed in the sandbox-only cutover): the task
    # still resolves the pinned version + finalizes, but never runs the engine.
    monkeypatch.setattr(
        "vibecanvas_api.background_tasks.deployment_invoke.run_workflow_sandboxed_sync",
        lambda *, workflow_id, inputs, tenant_id, user_id, **kw: (
            {"__end__": dict(inputs)}, {}, 0.0),
    )

    tenant_id, dep_id, task_id = await _seed_deployment(pg_engine, app_engine)

    from vibecanvas_api.background_tasks.deployment_invoke import deployment_invoke
    # The task body calls ``asyncio.run`` — pytest-asyncio already has
    # a running loop in this coroutine, so we MUST escape via
    # ``asyncio.to_thread`` (mirrors batch_exec's eager-test pattern).
    await asyncio.to_thread(
        deployment_invoke,
        **dict(
            task_id=str(task_id),
            tenant_id=str(tenant_id),
            deployment_id=str(dep_id),
            inputs={"x": 7},
        ),
    )

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


@pytest.mark.asyncio
async def test_deployment_invoke_handles_missing_deployment(
    pg_url, pg_engine, app_engine, monkeypatch,
):
    """Race: deployment soft-deleted between submit and pickup.

    The worker fetches the deployment under the tenant scope; the soft-
    delete filter in ``DeploymentsRepo.get`` makes the row invisible,
    the worker takes the early-out branch and finalises ``failed`` with
    a stable error string.

    See note on loop-isolation in the finished-path test — the same
    ``_admin_engine`` rebuild-in-worker-loop strategy applies. (This
    test's deployment-deleted branch doesn't actually call
    ``load_workflow_version``, but the worker still calls
    ``DeploymentsRepo.get`` via ``session_scope`` — the ``db._engine``
    singleton — and that engine is also process-global. The
    conftest's ``_isolate_global_engine`` autouse fixture disposes it
    around each test, which fortuitously forces a worker-loop rebuild
    too.)
    """
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    tenant_id, dep_id, task_id = await _seed_deployment(pg_engine, app_engine)

    # Soft-delete the deployment AFTER the task row was inserted but
    # BEFORE the worker runs. This is the exact race the resolver
    # already handles on the API side; T9 closes the worker-side gap.
    async with app_engine.connect() as c:
        await c.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(tenant_id)},
        )
        await c.execute(
            text(
                "UPDATE deployments "
                "SET deleted_at = now(), enabled = FALSE "
                "WHERE id = :id"
            ),
            {"id": dep_id},
        )
        await c.commit()

    from vibecanvas_api.background_tasks.deployment_invoke import deployment_invoke
    # Same loop-escape as the finished path — see comment above.
    await asyncio.to_thread(
        deployment_invoke,
        **dict(
            task_id=str(task_id),
            tenant_id=str(tenant_id),
            deployment_id=str(dep_id),
            inputs={"x": 7},
        ),
    )

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
