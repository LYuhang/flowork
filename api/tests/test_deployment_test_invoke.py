"""Deployments T12 — POST /api/v1/deployments/{id}/test-invoke.

Dashboard "test invoke": same execution path as the public ``/invoke``
endpoint, but authenticated via the user's session (handled by
``current_user`` upstream — bypassed here since we call the handler
directly) and RLS-scoped to the user's tenant.

Strategy mirrors ``test_deployment_invoke_sync.py``:

* Seed tenant + user via the superuser ``pg_engine`` (RLS-bypass for
  the auth tables).
* Seed workflow + workflow_versions + deployments via ``app_engine``
  under a ``set_config('app.tenant_id', ...)`` so RLS policies apply
  on INSERT.
* Monkeypatch ``_admin_engine`` to ``None`` and ``ADMIN_DATABASE_URL``
  to ``pg_url`` so ``load_workflow_version`` (which uses
  ``session_scope_admin``) can find the row.
* Call the handler directly with a ``session_scope`` opened under the
  caller's tenant.

The minimal workflow uses the engine-mandated ``__start__`` / ``__end__``
node names and ``workflow_name`` / ``workflow_description`` meta keys
(see engine/nodes/start.py:58 + engine/nodes/end.py:34); the variant
in the original task spec (``start`` / ``end``, ``name`` / ``description``)
would trip engine validation.
"""
from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.authorization.types import Decision
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


_MINIMAL_WORKFLOW = {
    "node_1": {
        "node_id": "node_1",
        "node_type": "StartNode",
        "node_name": "__start__",
        "node_description": "",
        "input_fields": {"x": {"type": "int", "value": 0, "reference": ""}},
        "output_fields": {"x": {"type": "int", "description": ""}},
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
    "__meta__": {"workflow_name": "min", "workflow_description": ""},
}


async def _seed_deployment(pg_engine, app_engine, *, tenant_id=None, user_id=None):
    """Returns ``(tenant_id, user_id, dep_id)``.

    ``tenant_id`` / ``user_id`` are pre-mintable so a second tenant can
    be created without re-seeding the workflow it doesn't own.
    """
    if tenant_id is None:
        tenant_id = uuid.uuid4()
    if user_id is None:
        user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
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
                "e": f"t12-{uuid.uuid4().hex[:6]}@example.com",
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
                ":id, :t, :u, :u, :w, 'T', :s, "
                "'api', 'specific', 1, 0, :h"
                ")"
            ),
            {
                "id": dep_id, "t": tenant_id, "u": user_id, "w": wf_id,
                "s": f"t-{uuid.uuid4().hex[:6]}",
                "h": hashlib.sha256(
                    f"k-{uuid.uuid4().hex[:8]}".encode()
                ).hexdigest(),
            },
        )
    return tenant_id, user_id, dep_id


class _Ctx:
    """Tiny stand-in for AuthContext when calling the handler directly
    (bypassing FastAPI ``Depends``). The handler doesn't read these
    attributes — the session is what carries the tenant binding via RLS
    — but we keep the shape parallel to ``AuthContext`` for clarity."""

    def __init__(self, tenant_id, user_id):
        self.tenant_id = str(tenant_id)
        self.user_id = str(user_id)
        self.active_organization_id = str(tenant_id)
        self.session_id = "test-session"
        self.session_generation = 1
        self.membership_id = "test-membership"
        self.membership_role = "owner"
        self.membership_status = "active"
        self.authentication_strength = "password"
        self.email = "deployment-invoke-test@example.invalid"


class _StubRequest:
    def __init__(self):
        self.headers = {}
        self.client = None
        self.state = SimpleNamespace(request_id="test-request")
        self.app = SimpleNamespace(state=SimpleNamespace(openfga_client=None))


class _AllowAuthz:
    async def check(self, *args, **kwargs):
        return Decision(allowed=True, reason_code="test_fixture")


@pytest.mark.asyncio
async def test_test_invoke_runs_and_returns_outputs(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    from vibecanvas_api.routes.deployments import test_invoke
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    # Orchestration test — MOCK the runner (a). This tests the dashboard
    # test-invoke ROUTE contract (resolve → run → return {outputs, exec_time_ms}),
    # NOT engine output. Replacing the sandbox runner with a canned echo
    # decouples the test from the in-process host-fallback (removed in the
    # sandbox-only cutover) and needs no gVisor.
    monkeypatch.setattr(
        "vibecanvas_api.routes.deployments.run_workflow_sandboxed_sync",
        lambda *, workflow_id, inputs, tenant_id, user_id, **kw: (
            {"__end__": dict(inputs)}, {}, 0.0),
    )

    tenant_id, user_id, dep_id = await _seed_deployment(pg_engine, app_engine)
    from tests.test_deployment_invoke_sync import _activate_test_revision
    await _activate_test_revision(tenant_id, dep_id)
    ctx = _Ctx(tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await test_invoke(
            dep_id=dep_id, body={"x": 5}, request=_StubRequest(), ctx=ctx,
            session=s, service=_AllowAuthz(),
        )
    assert "outputs" in resp
    assert "exec_time_ms" in resp
    assert isinstance(resp["outputs"], dict)
    assert resp["exec_time_ms"] >= 0


@pytest.mark.asyncio
async def test_test_invoke_other_tenant_404(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    """RLS — user B can't test-invoke A's deployment (returns 404)."""
    from vibecanvas_api.routes.deployments import test_invoke
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    tenant_a, _, dep_a = await _seed_deployment(pg_engine, app_engine)
    # Seed a second tenant + user (no workflow/dep needed — they're
    # trying to access A's dep_id).
    tenant_b = uuid.uuid4()
    user_b = uuid.uuid4()
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'b')"),
            {"t": tenant_b},
        )
        await c.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:u, :t, :e)"
            ),
            {
                "u": user_b, "t": tenant_b,
                "e": f"tb-{uuid.uuid4().hex[:6]}@example.com",
            },
        )

    ctx = _Ctx(tenant_b, user_b)
    async with session_scope(tenant_id=str(tenant_b)) as s:
        with pytest.raises(HTTPException) as exc:
            await test_invoke(
                dep_id=dep_a, body={"x": 1}, request=_StubRequest(), ctx=ctx,
                session=s, service=_AllowAuthz(),
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_test_invoke_disabled_404(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    from vibecanvas_api.routes.deployments import test_invoke
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    tenant_id, user_id, dep_id = await _seed_deployment(pg_engine, app_engine)
    async with app_engine.connect() as c:
        await c.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(tenant_id)},
        )
        await c.execute(
            text("UPDATE deployments SET enabled = FALSE WHERE id = :id"),
            {"id": dep_id},
        )
        await c.commit()

    ctx = _Ctx(tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        with pytest.raises(HTTPException) as exc:
            await test_invoke(
                dep_id=dep_id, body={}, request=_StubRequest(), ctx=ctx,
                session=s, service=_AllowAuthz(),
            )
        assert exc.value.status_code == 404


def test_route_mounted():
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts
    app = build_app()
    paths = {r.path for r in application_route_contexts(app)}
    assert any("/deployments/{dep_id}/test-invoke" in p for p in paths), (
        f"test-invoke missing; got {sorted(paths)}"
    )
