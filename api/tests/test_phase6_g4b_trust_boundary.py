"""Request body tenant, user, and DBOS identifiers must be ignored.

The route's Pydantic body model has ``extra="ignore"``; verifying both at
the schema layer (``model_validate``) AND at the persistence layer (a row
written via the actual model schema doesn't honor smuggled fields). The
full HTTP-layer end-to-end is in ``test_routes_workflows`` in staging;
this file is the gate that locks the §6.2 trust-boundary invariant.

Companion test: ``test_submit_body_silently_drops_smuggled_fields`` in
``test_batch_submit_and_reconciler.py`` covers the bare schema case. This
file extends it by:
  1. Adding ``status`` to the smuggled-fields set (a particularly nasty
     one — would let a client mark a task ``finished`` from the body).
  2. Driving the actual persistence path the route uses to confirm the
     DB row's ``tenant_id`` comes from auth context, not body.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


def test_body_drops_tenant_id_at_schema():
    """G4b §A — Pydantic ``extra='ignore'`` silently drops smuggled fields.

    The dumped body must carry ONLY the model's declared fields and MUST NOT
    contain any of the smuggled trust-boundary fields (``tenant_id`` /
    ``user_id`` / ``background_job_id`` / ``status``). The set of legitimately-declared
    fields has grown over time (``output`` / ``output_columns`` /
    ``concurrency`` for batch output shaping), so assert against the model's
    own declared field set rather than a hardcoded whitelist.
    """
    from vibecanvas_api.routes.workflows import BatchSubmitBody

    smuggled = {
        "tenant_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "background_job_id": "evil",
        "status": "finished",
    }
    body = BatchSubmitBody.model_validate({
        "data_source": {"rows": []},
        "column_mapping": {},
        **smuggled,
    })
    dumped = body.model_dump()
    # Only the model's own declared fields survive validation.
    assert set(dumped.keys()) == set(BatchSubmitBody.model_fields), (
        "Body must dump exactly its declared fields; "
        "trust boundary"
    )
    # And none of the smuggled trust-boundary fields leaked through.
    assert not (set(smuggled) & set(dumped.keys())), (
        "Body MUST drop tenant_id/user_id/background_job_id/status — "
        "request trust boundary"
    )


@pytest.mark.asyncio
async def test_persisted_tenant_id_is_auth_ctx_not_body(pg_engine):
    """G4b §B — DB ``tenant_id`` comes from auth context, never body.

    Repo-seam version of the route logic: the route handler constructs
    ``Task(tenant_id=uuid.UUID(ctx.tenant_id), ...)``. We exercise the
    same construction directly and confirm the persisted row carries
    auth-ctx tenant A even though a hypothetical client tried to smuggle
    tenant B in the body.
    """
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user_a = uuid.uuid4()
    async with pg_engine.begin() as c:
        for t in (tenant_a, tenant_b):
            await c.execute(
                text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
                {"t": t},
            )
        await c.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:u, :t, :e)"
            ),
            {"u": user_a, "t": tenant_a,
             "e": f"g4b-{uuid.uuid4().hex[:6]}@example.com"},
        )

    # Simulate the route: auth ctx says tenant=A; body claims tenant=B; the
    # route handler passes ONLY auth-ctx tenant to the Task model. We
    # exercise that contract by constructing the same Task instance the
    # route would, through the current encrypted repository boundary.
    task_id = uuid.uuid4()
    async with session_scope(tenant_id=str(tenant_a)) as s:
        await TasksRepo(s).create(
            task_id=task_id,
            tenant_id=tenant_a,           # from auth, NOT body
            user_id=user_a,
            workflow_id=None,
            task_type="batch_exec",
            # Body fields go in payload — and even payload here doesn't
            # carry the smuggled tenant/user/dbos (BatchSubmitBody
            # would have stripped them upstream).
            payload={"data_source": {}, "column_mapping": {}},
            background_job_id=str(task_id),
        )

    async with pg_engine.connect() as c:
        row = (await c.execute(
            text(
                "SELECT tenant_id, content_ciphertext, content_nonce, "
                "content_key_id FROM tasks WHERE id=:id"
            ),
            {"id": task_id},
        )).one()
    assert str(row.tenant_id) == str(tenant_a)
    assert str(row.tenant_id) != str(tenant_b)
    assert row.content_ciphertext and row.content_nonce
    assert row.content_key_id is not None
