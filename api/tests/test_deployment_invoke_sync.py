"""Deployments T6 — POST /deployments/{slug}/invoke (true sync in API process).

Coverage:

* End-to-end happy path: resolve → load → run → return outputs.
* Missing bearer → 401.
* Wrong api_key → 404.
* Disabled deployment → 404.
* api_key matches deployment A, URL slug is deployment B → 404.
* Router is mounted in ``build_app()``.

Strategy mirrors ``test_deployments_repo_and_service.py``: seed
tenant + user via ``pg_engine`` (superuser, RLS-bypass), then seed
the workflow + workflow_versions + deployments rows via ``app_engine``
(non-superuser, RLS-bound) with explicit ``set_config('app.tenant_id', ...)``.
``_admin_engine`` is monkeypatched onto ``pg_engine`` so the resolver's
``session_scope_admin`` actually finds the row (the prod admin role
bypasses RLS; the test superuser does the same).

We call the route handler directly (not through HTTPX) because:
1. The deployment-invoke flow doesn't need the JWT auth dependency
   that the test client conftest would wire in.
2. Driving the engine through ``Workflow.astream`` requires a real
   event loop, which ``pytest.mark.asyncio`` already supplies — no
   need for an ASGI roundtrip just to wrap that.
"""
from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


# A minimal StartNode-to-EndNode workflow. StartNode echoes
# its inputs into ``previous_outputs[node_1.x]``; EndNode is a sink.
# Engine constraints (engine/nodes/start.py + end.py):
#   * StartNode node_name must be '__start__', children <= 1.
#   * EndNode node_name must be '__end__'.
_MINIMAL_WORKFLOW = {
    "node_1": {
        "node_id": "node_1",
        "node_type": "StartNode",
        "node_name": "__start__",
        "node_description": "",
        "input_fields": {
            "x": {"type": "integer", "value": 0, "reference": ""},
        },
        "output_fields": {
            "x": {"type": "integer", "description": ""},
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
            "y": {"type": "integer", "value": 0, "reference": "__start__.x"},
        },
        "output_fields": {"y": {"type": "integer", "description": ""}},
        "node_config": {},
        "children": [],
        "__attributes__": {"x": 200, "y": 0},
    },
    "__meta__": {
        "workflow_name": "min",
        "workflow_description": "",
    },
}


def _versioned_workflow(multiplier: int) -> dict:
    """A Start → Transform → End workflow whose End output is the input
    ``x`` multiplied by ``multiplier``.

    Used to make two versions produce DIFFERENT outputs for the SAME
    input so the pinned-version test can prove which version actually
    ran. The mocked runner (``_fake_sandboxed_sync``) recovers the
    multiplier from the workflow_dict it is HANDED and echoes
    ``x * multiplier`` — so the pin assertion still catches a runner that
    loaded the wrong (head) version (it would receive the wrong content).
    """
    return {
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
            "node_type": "TransformNode",
            "node_name": "scale",
            "node_description": "",
            "input_fields": {
                "x": {"type": "int", "value": 0, "reference": "__start__.x"},
            },
            "output_fields": {
                "out": {"type": "int", "description": ""},
            },
            "node_config": {
                "mappings": [
                    {
                        "input_field": "x",
                        "output_field": "out",
                        "transform_list": [
                            {"op": "compute", "expr": f"{{value}} * {multiplier}"},
                            {"op": "cast", "to": "integer"},
                        ],
                    },
                ],
            },
            "children": ["node_3"],
            "__attributes__": {"x": 200, "y": 0},
        },
        "node_3": {
            "node_id": "node_3",
            "node_type": "EndNode",
            "node_name": "__end__",
            "node_description": "",
            "input_fields": {
                "out": {"type": "int", "value": 0, "reference": "scale.out"},
            },
            "output_fields": {
                "out": {"type": "int", "description": ""},
            },
            "node_config": {},
            "children": [],
            "__attributes__": {"x": 400, "y": 0},
        },
        "__meta__": {
            "workflow_name": f"scale-x{multiplier}",
            "workflow_description": "",
        },
    }


# ---------------------------------------------------------- mocked runner (a)
#
# These tests exercise the synchronous deployment route and version-
# pin (resolve → load → run → return / 401 / 404), NOT engine output. So we
# MOCK ``run_workflow_sandboxed_sync`` rather than driving the engine through
# the (about-to-be-removed) in-process host-fallback. The mock is faithful to
# the one assertion that matters — the pin test asserts WHICH workflow_dict
# ran — by deriving the End output from the workflow content it is HANDED
# (``workflow_dict``), so a runner that loaded the wrong (head) version would
# still be caught: it would receive the wrong content and echo the wrong number.


def _transform_multiplier(workflow_dict: dict) -> int | None:
    """Recover the ``_versioned_workflow`` multiplier from a workflow dict by
    parsing its TransformNode ``compute`` expr (``{value} * <m>``). Returns
    ``None`` for the minimal (no-transform) workflow."""
    for node in workflow_dict.values():
        if not isinstance(node, dict) or node.get("node_type") != "TransformNode":
            continue
        for mp in node.get("node_config", {}).get("mappings", []):
            for step in mp.get("transform_list", []):
                if step.get("op") == "compute":
                    expr = step.get("expr", "")
                    # "{value} * 1000" → 1000
                    return int(expr.split("*")[-1].strip())
    return None


def _fake_sandboxed_sync(*, workflow_id, inputs, tenant_id, user_id,
                         workflow_dict=None, run_id=None, **kw):
    """Echo runner: compute the End output from the HANDED ``workflow_dict``.

    * versioned (Start→Transform→End) wf → ``{"__end__": {"out": x * mult}}``
      where ``mult`` is parsed from THAT dict (proves which version ran).
    * minimal (Start→End) wf → ``{"__end__": {}}`` (happy-path shape only).
    """
    mult = _transform_multiplier(workflow_dict or {})
    if mult is not None:
        x = int(inputs.get("x", 0))
        return ({"__end__": {"out": x * mult}}, {}, 0.0)
    return ({"__end__": {}}, {}, 0.0)


@pytest.fixture
def mock_runner(monkeypatch):
    """Patch the sync runner at its point of use (the route module) so route
    tests never touch the engine / sandbox / in-process fallback."""
    async def dispatch(*, dep, workflow, inputs, tenant_id, invocation_id, **kwargs):
        from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
        outputs, errors, elapsed = _fake_sandboxed_sync(
            workflow_id=dep["wf_id"], workflow_dict=workflow, inputs=inputs,
            tenant_id=tenant_id, user_id=dep["user_id"], run_id=invocation_id,
        )
        async with session_scope(tenant_id=tenant_id) as session:
            history = WorkflowHistoryRepo(session)
            await history.bind_runtime(invocation_id, "test-runtime")
            await history.persist_events(invocation_id, "test-runtime", [{
                "seq": 1, "generation": "test-runtime", "invocation_id": invocation_id,
                "type": "result", "status": "succeeded", "final_outputs": outputs,
                "error_dict": errors, "execution_time": elapsed,
            }])
            from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
            await DeploymentInvocationsRepo(session).mark_terminal(uuid.UUID(invocation_id), status="succeeded", latency_ms=elapsed*1000)
    monkeypatch.setattr("vibecanvas_api.routes.deployment_invoke._dispatch_invocation", dispatch)
    yield


# --------------------------------------------------------------------- seed


async def _seed_full_deployment(pg_engine, app_engine, *, enabled: bool = True):
    """Seed tenant + user + workflow + workflow_versions + deployment.

    Returns a tuple ``(tenant_id, slug, api_key_plaintext, dep_id)``.
    The api_key plaintext is the value the caller passes in the Bearer
    header; its SHA-256 hash is what we INSERT into ``api_key_hash``.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    api_key = f"vc_test_{uuid.uuid4().hex[:12]}"
    h = hashlib.sha256(api_key.encode()).hexdigest()
    slug = f"sync-{uuid.uuid4().hex[:6]}"
    dep_id = uuid.uuid4()

    # Tenant + user via superuser engine (RLS-bypass; auth tables aren't
    # RLS-scoped but using pg_engine is simpler than configuring GUCs).
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
                "e": f"t6-{uuid.uuid4().hex[:6]}@example.com",
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
                "api_key_hash, enabled"
                ") VALUES ("
                ":id, :t, :u, :u, :w, 'Sync', :s, "
                "'api', 'specific', 1, 0, :h, :en"
                ")"
            ),
            {
                "id": dep_id, "t": tenant_id, "u": user_id, "w": wf_id,
                "s": slug, "h": h, "en": enabled,
            },
        )
    return tenant_id, slug, api_key, dep_id


async def _activate_test_revision(tenant_id, dep_id):
    """Publish a resolved resident revision for route tests with a mocked runner."""
    import json
    from vibecanvas_api.services.deployment_revisions import execution_spec
    from vibecanvas_api.services.deployment_snapshots import resolve_workflow
    async with session_scope(tenant_id=str(tenant_id)) as session:
        dep = dict((await session.execute(text('SELECT * FROM deployments WHERE id=:id'), {'id': dep_id})).mappings().one())
        graph = await resolve_workflow(session, dep['user_id'], dep)
        spec = execution_spec(dep, graph, str(dep['user_id']))
        revision = uuid.uuid4()
        await session.execute(text("""INSERT INTO deployment_runtime_revisions(id,tenant_id,deployment_id,spec,state)
            VALUES (:id,:tenant,:dep,CAST(:spec AS jsonb),'active')"""),
            {'id': revision, 'tenant': tenant_id, 'dep': dep_id, 'spec': json.dumps(spec)})
        await session.execute(text("UPDATE deployments SET active_revision_id=:revision, rollout_status='ready' WHERE id=:id"),
            {'revision': revision, 'id': dep_id})


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_invoke_sync_returns_outputs(
    pg_engine, app_engine, monkeypatch, mock_runner,
):
    """End-to-end (no HTTP roundtrip): resolve → load → run → return.

    The minimal workflow yields ``previous_outputs`` keyed by node id;
    we do not assert exact contents because the engine event shape is
    canonical and changes touch a separate test surface), but
    ``outputs`` must be a dict and ``exec_time_ms`` a non-negative
    float.
    """
    from vibecanvas_api.routes.deployment_invoke import invoke_sync
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    tenant, slug, api_key, dep_id = await _seed_full_deployment(pg_engine, app_engine)
    await _activate_test_revision(tenant, dep_id)
    result = await invoke_sync(
        slug=slug, body={"x": 21},
        authorization=f"Bearer {api_key}",
    )
    assert isinstance(result, dict), (
        f"expected dict (success path), got {type(result).__name__}"
    )
    assert "outputs" in result
    assert "exec_time_ms" in result
    assert isinstance(result["outputs"], dict)
    assert result["exec_time_ms"] >= 0


@pytest.mark.asyncio
async def test_invoke_rejects_missing_bearer(pg_engine, app_engine, monkeypatch):
    """No ``Authorization`` header → 401 (RFC compliance)."""
    from vibecanvas_api.routes.deployment_invoke import invoke_sync
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, _, _ = await _seed_full_deployment(pg_engine, app_engine)
    with pytest.raises(HTTPException) as exc:
        await invoke_sync(slug=slug, body={}, authorization=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_invoke_rejects_bad_key(pg_engine, app_engine, monkeypatch):
    """Unknown api_key → 404 (uniform existence-leak guard)."""
    from vibecanvas_api.routes.deployment_invoke import invoke_sync
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, _, _ = await _seed_full_deployment(pg_engine, app_engine)
    with pytest.raises(HTTPException) as exc:
        await invoke_sync(
            slug=slug, body={}, authorization="Bearer vc_wrong_key",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_invoke_rejects_disabled_deployment(
    pg_engine, app_engine, monkeypatch,
):
    """``enabled = FALSE`` → 404. Key is valid but the deployment is
    paused; we must NOT run the workflow."""
    from vibecanvas_api.routes.deployment_invoke import invoke_sync
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    tenant_id, slug, api_key, dep_id = await _seed_full_deployment(
        pg_engine, app_engine,
    )
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

    with pytest.raises(HTTPException) as exc:
        await invoke_sync(
            slug=slug, body={}, authorization=f"Bearer {api_key}",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_invoke_rejects_wrong_slug(pg_engine, app_engine, monkeypatch):
    """API key matches deployment A, but URL slug is for an unrelated
    deployment B (here just a random non-existent slug). Must 404 —
    a key holder for A cannot invoke B's slug even if both exist."""
    from vibecanvas_api.routes.deployment_invoke import invoke_sync
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, _, api_key, _ = await _seed_full_deployment(pg_engine, app_engine)
    with pytest.raises(HTTPException) as exc:
        await invoke_sync(
            slug="some-other-slug",
            body={},
            authorization=f"Bearer {api_key}",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_invoke_honors_pinned_version_not_head(
    pg_engine, app_engine, monkeypatch, mock_runner,
):
    """Spec §6.4 regression guard: a deployment pinned to a SPECIFIC
    (non-head) version must run THAT version, not the current/head one.

    Seed two distinct-output versions of the same workflow:
      * v1.sv0 (PINNED): out = x * 1   → for x=21, end output == 21
      * v1.sv1 (HEAD/newer): out = x * 1000 → for x=21, end output == 21000
    The deployment pins ``specific`` major=1 sub=0. Invoking it must
    yield 21 (v1), proving the pinned version — NOT the head — ran.
    """
    from vibecanvas_api.routes.deployment_invoke import invoke_sync
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    api_key = f"vc_test_{uuid.uuid4().hex[:12]}"
    h = hashlib.sha256(api_key.encode()).hexdigest()
    slug = f"pin-{uuid.uuid4().hex[:6]}"
    dep_id = uuid.uuid4()

    v1 = _versioned_workflow(multiplier=1)
    v2 = _versioned_workflow(multiplier=1000)

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
            {"u": user_id, "t": tenant_id, "e": f"pin-{uuid.uuid4().hex[:6]}@example.com"},
        )

    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = WorkflowRepo(session, str(user_id))
        await repo.create_workflow(
            wf_id=wf_id,
            name="Pinned Workflow",
            initial_workflow=v1,
        )
        await repo.commit(wf_id, v2, note="new head")
        # Deployment pins SPECIFIC major=1 sub=0 (== v1).
        await session.execute(
            text(
                "INSERT INTO deployments("
                "id, tenant_id, user_id, owner_id, wf_id, name, slug, "
                "trigger_type, version_pin, pinned_major, pinned_sub, "
                "api_key_hash, enabled"
                ") VALUES ("
                ":id, :t, :u, :u, :w, 'Pinned', :s, "
                "'api', 'specific', 1, 0, :h, TRUE"
                ")"
            ),
            {"id": dep_id, "t": tenant_id, "u": user_id, "w": wf_id, "s": slug, "h": h},
        )

    await _activate_test_revision(tenant_id, dep_id)
    result = await invoke_sync(
        slug=slug, body={"x": 21}, authorization=f"Bearer {api_key}",
    )
    assert isinstance(result, dict), (
        f"expected dict (success path), got {type(result).__name__}: {result}"
    )
    end_out = result["outputs"]["out"]
    assert end_out == 21, (
        f"deployment pinned to v1 (x*1) must run v1 → 21, but got {end_out}. "
        f"A value of 21000 means the runner silently ran the HEAD version "
        f"(v1.sv1, x*1000) instead of the pinned version."
    )


def test_router_mounted_in_app():
    """The deployment-invoke router is registered in ``build_app()`` so
    the route is reachable through the live ASGI app (defence-in-depth
    against a stale import that compiles but never mounts).
    """
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts
    app = build_app()
    paths = {r.path for r in application_route_contexts(app)}
    assert any("/deployments/{slug}/invoke" in p for p in paths), (
        f"invoke route missing; got {sorted(paths)}"
    )
