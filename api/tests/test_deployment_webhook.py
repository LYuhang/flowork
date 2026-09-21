"""Deployments T8 — POST /deployments/{slug}/webhook HMAC + size guard.

Coverage:

* Valid signature + 202 task_id return; invocation ids are not Task rows.
  with payload wrapped as ``{"payload": <parsed body>}``.
* Invalid signature → 401.
* Expired timestamp (>300s old) → 401.
* Advertised or actual body larger than 1 MiB → 413.
* A valid delivery replay returns the original invocation without re-enqueue.
* Wrong ``Content-Type`` → 415.
* Malformed JSON body (after sig passes) → 400.
* Unknown slug → 404.
* Router is mounted in ``build_app()``.

Strategy mirrors T7 (``test_deployment_runs_async.py``): seed
tenant + user via the superuser ``pg_engine`` (auth tables aren't
RLS-scoped), then seed workflow + workflow_versions + deployments
via ``app_engine`` (RLS-bound) with an explicit
``set_config('app.tenant_id', ...)``. ``_admin_engine`` is
monkeypatched onto ``pg_engine`` so
``resolve_deployment_and_bind_tenant``'s ``session_scope_admin``
can find the row.

DBOS ``send_task`` is stubbed: T9 ships the worker; here we
assert only the API-side row insert + task_id return. We call
the route handler directly with a stub ``Request`` — the
webhook reads ``.headers`` and ``.body()`` only, so a minimal
stand-in suffices and we avoid wiring TestClient.
"""
from __future__ import annotations

import hmac
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


# Minimal workflow content — only needs to validate as JSON for the
# ``workflow_versions.workflow`` JSONB column. The webhook-submit
# path does NOT load or run it; only T9's worker will.
_MINIMAL_WORKFLOW = {
    "node_1": {
        "node_id": "node_1",
        "node_type": "StartNode",
        "node_name": "__start__",
        "node_description": "",
        "input_fields": {
            "payload": {"type": "dict", "value": {}, "reference": ""},
        },
        "output_fields": {
            "payload": {"type": "dict", "description": ""},
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
            "out": {"type": "dict", "value": {}, "reference": "__start__.payload"},
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


def _sign(secret: str, ts: str, body: bytes) -> str:
    """Compute the canonical ``sha256=...`` signature header value."""
    return "sha256=" + hmac.new(
        secret.encode(), ts.encode() + b"." + body, "sha256",
    ).hexdigest()


class _StubRequest:
    """Minimal ``starlette.Request`` stand-in.

    The webhook handler only reads ``.headers`` and ``await
    .body()`` — no need to wire ASGI scope. Headers is a plain
    dict (handler uses ``.get(key, default)`` which works on
    dicts and Starlette ``Headers`` alike).
    """

    def __init__(self, *, headers: dict, body: bytes):
        self.headers = headers
        self._body = body

    async def body(self) -> bytes:
        return self._body


# --------------------------------------------------------------------- seed


async def _seed_webhook_dep(pg_engine, app_engine):
    """Seed tenant + user + workflow + workflow_versions + a
    webhook-trigger deployment.

    Returns ``(tenant_id, slug, hmac_secret, dep_id)``. The plaintext secret
    exists only in this test process; persistence uses the same opaque
    SecretService reference as production.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    secret = f"whsec_t8_{uuid.uuid4().hex[:24]}"
    slug = f"hook-{uuid.uuid4().hex[:6]}"
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
                "e": f"t8-{uuid.uuid4().hex[:6]}@example.com",
            },
        )

    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=wf_id,
            name="min",
            creator_user_id=str(user_id),
            initial_workflow=_MINIMAL_WORKFLOW,
        )
        secret_ref = await secret_service().put_text(
            session,
            tenant_id=tenant_id,
            purpose="deployment_webhook_hmac",
            resource_type="deployment",
            resource_id=dep_id,
            plaintext=secret,
        )
        await DeploymentsRepo(session).insert(
            id=dep_id,
            tenant_id=tenant_id,
            user_id=user_id,
            owner_id=user_id,
            wf_id=wf_id,
            name="Hook",
            slug=slug,
            trigger_type="webhook",
            version_pin="head",
            pinned_major=None,
            pinned_sub=None,
            hmac_secret_ref=secret_ref,
            hmac_secret_version=1,
        )
    return tenant_id, slug, secret, dep_id


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_webhook_valid_signature_accepts(
    pg_engine, app_engine, monkeypatch,
):
    """Valid HMAC + timestamp + size + content-type → 202 + task_id.

    Deployment webhook invocations are tracked by Deployment observability, not
    the global Task Center.
    """
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.services import deployments_service
    from vibecanvas_api.storage import db as db_mod

    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)
    # Stub DBOS send_task — T9 ships the worker; here we only assert
    # the API-side row insert.
    monkeypatch.setattr(
        deployments_service, "enqueue_background_job_in_transaction", AsyncMock(),
    )

    tenant_id, slug, secret, _ = await _seed_webhook_dep(
        pg_engine, app_engine,
    )
    payload = b'{"event": "ping"}'
    ts = str(int(time.time()))
    req = _StubRequest(
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-Vibecanvas-Signature": _sign(secret, ts, payload),
            "X-Vibecanvas-Timestamp": ts,
        },
        body=payload,
    )
    result = await webhook(slug=slug, request=req)
    assert "task_id" in result
    task_id = result["task_id"]

    # Deployment webhook invocations are not Task Center rows.
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
async def test_webhook_invalid_signature_rejects(
    pg_engine, app_engine, monkeypatch,
):
    """Wrong signature (right shape, wrong digest) → 401."""
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, _, _ = await _seed_webhook_dep(pg_engine, app_engine)
    payload = b"{}"
    ts = str(int(time.time()))
    req = _StubRequest(
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-Vibecanvas-Signature": "sha256=" + "0" * 64,
            "X-Vibecanvas-Timestamp": ts,
        },
        body=payload,
    )
    with pytest.raises(HTTPException) as exc:
        await webhook(slug=slug, request=req)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_webhook_expired_timestamp_rejects(
    pg_engine, app_engine, monkeypatch,
):
    """Timestamp >300s old → 401 (replay protection)."""
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, secret, _ = await _seed_webhook_dep(pg_engine, app_engine)
    payload = b"{}"
    # 400s old — outside the 300s window. The signature itself is
    # mathematically valid for this stale ts; only the window check
    # should reject it.
    ts = str(int(time.time()) - 400)
    req = _StubRequest(
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-Vibecanvas-Signature": _sign(secret, ts, payload),
            "X-Vibecanvas-Timestamp": ts,
        },
        body=payload,
    )
    with pytest.raises(HTTPException) as exc:
        await webhook(slug=slug, request=req)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_webhook_oversized_413(
    pg_engine, app_engine, monkeypatch,
):
    """``Content-Length`` >1 MiB → 413 BEFORE we read the body."""
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, secret, _ = await _seed_webhook_dep(pg_engine, app_engine)
    payload = b"x" * 100  # actual body is tiny — header lies
    ts = str(int(time.time()))
    req = _StubRequest(
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(1_048_577),  # >1 MiB advertised
            "X-Vibecanvas-Signature": _sign(secret, ts, payload),
            "X-Vibecanvas-Timestamp": ts,
        },
        body=payload,
    )
    with pytest.raises(HTTPException) as exc:
        await webhook(slug=slug, request=req)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_webhook_actual_body_limit_rejects_lying_content_length(
    pg_engine, app_engine, monkeypatch,
):
    """The actual byte count, not the untrusted header, owns the hard limit."""
    from vibecanvas_api.routes.deployment_invoke import webhook

    payload = b"x" * 1_048_577
    req = _StubRequest(
        headers={
            "Content-Type": "application/json",
            "Content-Length": "1",
            "X-Vibecanvas-Signature": "sha256=" + "0" * 64,
            "X-Vibecanvas-Timestamp": str(int(time.time())),
        },
        body=payload,
    )
    with pytest.raises(HTTPException) as exc:
        await webhook(slug="does-not-matter", request=req)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_webhook_replay_returns_original_invocation_without_reenqueue(
    pg_engine, app_engine, monkeypatch,
):
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.services import deployments_service
    from vibecanvas_api.storage import db as db_mod

    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)
    sent: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        deployments_service,
        "enqueue_background_job_in_transaction",
        AsyncMock(side_effect=lambda *args, **kwargs: sent.append((args, kwargs))),
    )
    tenant_id, slug, secret, deployment_id = await _seed_webhook_dep(
        pg_engine, app_engine,
    )
    payload = b'{"event":"once"}'
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
        "X-Vibecanvas-Signature": _sign(secret, ts, payload),
        "X-Vibecanvas-Timestamp": ts,
    }

    first = await webhook(
        slug=slug,
        request=_StubRequest(headers=headers, body=payload),
    )
    replay = await webhook(
        slug=slug,
        request=_StubRequest(headers=headers, body=payload),
    )

    assert replay == first
    assert len(sent) == 1
    async with app_engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(tenant_id)},
        )
        receipt_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM deployment_webhook_receipts "
                    "WHERE deployment_id = :deployment_id"
                ),
                {"deployment_id": deployment_id},
            )
        ).scalar_one()
        invocation_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM deployment_invocations "
                    "WHERE deployment_id = :deployment_id AND source = 'webhook'"
                ),
                {"deployment_id": deployment_id},
            )
        ).scalar_one()
    assert receipt_count == 1
    assert invocation_count == 1


@pytest.mark.asyncio
async def test_webhook_wrong_content_type_415(
    pg_engine, app_engine, monkeypatch,
):
    """Non-JSON ``Content-Type`` → 415 (refused before any DB lookup)."""
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, secret, _ = await _seed_webhook_dep(pg_engine, app_engine)
    payload = b"plain text"
    ts = str(int(time.time()))
    req = _StubRequest(
        headers={
            "Content-Type": "text/plain",
            "Content-Length": str(len(payload)),
            "X-Vibecanvas-Signature": _sign(secret, ts, payload),
            "X-Vibecanvas-Timestamp": ts,
        },
        body=payload,
    )
    with pytest.raises(HTTPException) as exc:
        await webhook(slug=slug, request=req)
    assert exc.value.status_code == 415


@pytest.mark.asyncio
async def test_webhook_malformed_json_400(
    pg_engine, app_engine, monkeypatch,
):
    """Valid HMAC + valid size + valid ct, but body isn't JSON → 400.

    The signature MUST pass before we even attempt parse — so we
    sign the raw non-JSON bytes and expect the JSON-parse layer to
    be the failing step.
    """
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    _, slug, secret, _ = await _seed_webhook_dep(pg_engine, app_engine)
    payload = b"not json"
    ts = str(int(time.time()))
    req = _StubRequest(
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-Vibecanvas-Signature": _sign(secret, ts, payload),
            "X-Vibecanvas-Timestamp": ts,
        },
        body=payload,
    )
    with pytest.raises(HTTPException) as exc:
        await webhook(slug=slug, request=req)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_webhook_unknown_slug_404(
    pg_engine, app_engine, monkeypatch,
):
    """Unknown slug → 404 (resolve returns None before sig check).

    We seed a deployment so we exercise the path that has rows in
    the DB; then call with a slug that doesn't exist.
    """
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)
    _, _, _, _ = await _seed_webhook_dep(pg_engine, app_engine)

    payload = b"{}"
    ts = str(int(time.time()))
    req = _StubRequest(
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-Vibecanvas-Signature": "sha256=0",
            "X-Vibecanvas-Timestamp": ts,
        },
        body=payload,
    )
    with pytest.raises(HTTPException) as exc:
        await webhook(slug="nonexistent-slug", request=req)
    assert exc.value.status_code == 404


def test_webhook_route_mounted():
    """``/{slug}/webhook`` is wired into ``build_app()`` (defence-in-depth
    against a stale import that compiles but never mounts)."""
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts
    app = build_app()
    paths = {r.path for r in application_route_contexts(app)}
    assert any("/deployments/{slug}/webhook" in p for p in paths), (
        f"webhook route missing; got {sorted(paths)}"
    )
