"""Deployments T4 — ``POST /api/v1/deployments`` create + one-shot secrets.

Strategy: direct handler calls matching the reconciler
resolver pattern). The plan's TestClient path is fragile against the
``current_user`` Bearer-token DI; the handler-direct approach exercises
the same body validation + secret generation + version-head resolution
+ tenant assertion without standing up the auth DI stack.

Coverage:
* Pydantic body validation: slug regex, API/webhook trigger enum,
  version_pin enum, and non-negative rate_limit_qps.
* G4b trust boundary: smuggled ``tenant_id`` / ``user_id`` /
  ``api_key_hash`` in the body are silently dropped by
  ``ConfigDict(extra='ignore')``.
* Create-api → returns ``api_key`` plaintext + endpoint_url; stores
  the SHA-256 hash (NOT the plaintext) in ``deployments.api_key_hash``.
* Create-webhook → returns ``hmac_secret`` plaintext + webhook_url.
* Version-head resolution: ``version_pin='specific'`` without pinned
  fields ⇒ defaults to current HEAD; 404 if no versions yet.
* Tenant binding: row created under ``ctx.tenant_id``, NOT body's
  ``tenant_id``.
* Router is mounted under ``/api/v1/deployments`` so external callers
  can reach the route through the ASGI app.
"""
from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from vibecanvas_api.routes.deployments import (
    CreateDeploymentBody, create_deployment,
)
from vibecanvas_api.authorization.types import Decision


# ---------------------------------------------------------------------- seed


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
             "e": f"t4-{uuid.uuid4().hex[:6]}@example.com"},
        )
        await c.execute(
            text(
                "INSERT INTO organizations("
                "tenant_id, kind, slug, name, created_by"
                ") VALUES (:t, 'personal', :slug, 'Test account', :u)"
            ),
            {
                "t": tenant_id,
                "u": user_id,
                "slug": f"test-{tenant_id.hex}",
            },
        )


async def _seed_workflow(app_engine, wf_id, tenant_id, user_id) -> None:
    """Insert a workflow under the tenant (RLS-bound, owns the row)."""
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo

    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=wf_id,
            name="Deployment Workflow",
        )


async def _seed_workflow_version(
    app_engine, wf_id, tenant_id, user_id, *, major: int, sub: int,
) -> None:
    """Create the requested test version through the encrypted repository."""
    if (major, sub) == (1, 0):
        return
    assert major == 2 and sub >= 0
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo

    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = WorkflowRepo(session, str(user_id))
        await repo.new_version(wf_id, {}, note="test v2")
        for index in range(sub):
            await repo.commit(
                wf_id,
                {},
                note=f"test v2.sv{index + 1}",
                target_major=major,
            )


class _StubCtx:
    """Lightweight stand-in for ``AuthContext``. The handler only reads
    ``tenant_id`` / ``user_id`` (both strings); ``email`` is unused."""

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
        self.email = "stub@example.com"


class _StubRequest:
    def __init__(self):
        self.headers = {}
        self.client = None
        self.state = SimpleNamespace(request_id="test-request")
        self.app = SimpleNamespace(
            state=SimpleNamespace(openfga_client=None),
        )
        self.scope = {
            "type": "http", "headers": [], "app": self.app,
            "state": {"request_id": "test-request"},
        }


@pytest.fixture(autouse=True)
def _workflow_scope_authorization(monkeypatch):
    # Handler tests stub authorization; the nested Workflow scope now obtains
    # its own service while retaining the real database admission/RLS checks.
    from vibecanvas_api.authorization import dependencies
    monkeypatch.setattr(dependencies, "get_authz_service", AsyncMock(return_value=_AllowAuthz()))


class _AllowAuthz:
    async def check(self, *args, **kwargs):
        return Decision(allowed=True, reason_code="test_fixture")


class _DenyAuthz:
    async def check(self, *args, **kwargs):
        return Decision(allowed=False, reason_code="test_fixture")


# -------------------------------------------------------------- pydantic-only


def test_create_body_drops_smuggled_identity_fields():
    """G4b — smuggled ``tenant_id`` / ``user_id`` / ``api_key_hash``
    / ``id`` are silently dropped by ``ConfigDict(extra='ignore')``."""
    body = CreateDeploymentBody.model_validate({
        "wf_id": "wf_x", "name": "Bot", "slug": "bot-x",
        "trigger_type": "api", "version_pin": "head",
        # All of the following MUST be silently dropped:
        "tenant_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "id": str(uuid.uuid4()),
        "api_key_hash": "deadbeef",
        "hmac_secret": "owned",
    })
    dumped = body.model_dump()
    assert "tenant_id" not in dumped
    assert "user_id" not in dumped
    assert "id" not in dumped
    assert "api_key_hash" not in dumped
    assert "hmac_secret" not in dumped


def test_slug_validation():
    import pydantic
    # Uppercase rejected
    with pytest.raises(pydantic.ValidationError):
        CreateDeploymentBody.model_validate({
            "wf_id": "wf_x", "name": "B", "slug": "Bot1",
            "trigger_type": "api", "version_pin": "head",
        })
    # Too long (>63 chars)
    with pytest.raises(pydantic.ValidationError):
        CreateDeploymentBody.model_validate({
            "wf_id": "wf_x", "name": "B", "slug": "a" * 70,
            "trigger_type": "api", "version_pin": "head",
        })
    # Leading hyphen rejected
    with pytest.raises(pydantic.ValidationError):
        CreateDeploymentBody.model_validate({
            "wf_id": "wf_x", "name": "B", "slug": "-bot",
            "trigger_type": "api", "version_pin": "head",
        })
    # Underscore rejected
    with pytest.raises(pydantic.ValidationError):
        CreateDeploymentBody.model_validate({
            "wf_id": "wf_x", "name": "B", "slug": "bot_1",
            "trigger_type": "api", "version_pin": "head",
        })
    # OK shape
    body = CreateDeploymentBody.model_validate({
        "wf_id": "wf_x", "name": "B", "slug": "bot-1",
        "trigger_type": "api", "version_pin": "head",
    })
    assert body.slug == "bot-1"


def test_trigger_type_validation():
    import pydantic
    for unsupported in ("ssh", "cron"):
        with pytest.raises(pydantic.ValidationError):
            CreateDeploymentBody.model_validate({
                "wf_id": "wf_x", "name": "B", "slug": "bot",
                "trigger_type": unsupported, "version_pin": "head",
            })


def test_version_pin_validation():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        CreateDeploymentBody.model_validate({
            "wf_id": "wf_x", "name": "B", "slug": "bot",
            "trigger_type": "api", "version_pin": "main",
        })


def test_rate_limit_qps_nonneg():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        CreateDeploymentBody.model_validate({
            "wf_id": "wf_x", "name": "B", "slug": "bot",
            "trigger_type": "api", "version_pin": "head",
            "rate_limit_qps": -1,
        })
    body = CreateDeploymentBody.model_validate({
        "wf_id": "wf_x", "name": "B", "slug": "bot",
        "trigger_type": "api", "version_pin": "head",
        "rate_limit_qps": 0,
    })
    assert body.rate_limit_qps == 0


# -------------------------------------------------------- handler-direct call


@pytest.mark.asyncio
async def test_create_api_deployment_returns_plaintext_key(
    pg_engine, app_engine,
):
    """``trigger_type='api'`` returns ``api_key`` plaintext + endpoint_url.

    Stored hash in DB == SHA-256(plaintext); plaintext itself is NOT
    persisted. The response does NOT include ``api_key_hash``.
    """
    from vibecanvas_api.storage.db import session_scope

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)

    ctx = _StubCtx(tenant_id, user_id)
    body = CreateDeploymentBody.model_validate({
        "wf_id": wf_id, "name": "API Bot", "slug": "api-bot",
        "trigger_type": "api", "version_pin": "head",
    })

    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await create_deployment(
            body=body, request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    assert "id" in resp
    plaintext = resp["api_key"]
    assert plaintext.startswith("vc_")
    assert resp["endpoint_url"] == "/api/v1/deployments/api-bot/invoke"
    # Critical secret-handling invariant: the hash MUST NOT appear in the
    # response (only the plaintext is one-shot-returned).
    assert "api_key_hash" not in resp
    # And we DON'T return the plaintext under a hash-coded name either.
    assert "hmac_secret" not in resp

    # DB stores the SHA-256 of the plaintext, not the plaintext.
    expected_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    async with pg_engine.connect() as c:
        row = (await c.execute(
            text(
                "SELECT api_key_hash, hmac_secret_ref, tenant_id, user_id, "
                "trigger_type, slug FROM deployments WHERE id = :id"
            ),
            {"id": uuid.UUID(resp["id"])},
        )).one()
    assert row.api_key_hash == expected_hash
    assert row.hmac_secret_ref is None
    assert row.tenant_id == tenant_id      # came from ctx, not body
    assert row.user_id == user_id          # came from ctx, not body
    assert row.trigger_type == "api"
    assert row.slug == "api-bot"


@pytest.mark.asyncio
async def test_create_webhook_returns_hmac_secret(pg_engine, app_engine):
    """``trigger_type='webhook'`` returns ``hmac_secret`` + webhook_url.

    The one-shot plaintext is envelope encrypted; the deployment row retains
    only its SecretService reference.
    """
    from vibecanvas_api.storage.db import session_scope

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)

    ctx = _StubCtx(tenant_id, user_id)
    body = CreateDeploymentBody.model_validate({
        "wf_id": wf_id, "name": "Hook", "slug": "hook-x",
        "trigger_type": "webhook", "version_pin": "head",
    })

    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await create_deployment(
            body=body, request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    secret = resp["hmac_secret"]
    assert secret.startswith("whsec_")
    assert resp["webhook_url"] == "/api/v1/deployments/hook-x/webhook"
    assert "api_key" not in resp

    # The business table never stores the verifier plaintext.
    async with pg_engine.connect() as c:
        row = (await c.execute(
            text(
                "SELECT api_key_hash, hmac_secret_ref, "
                "trigger_type "
                "FROM deployments WHERE id = :id"
            ),
            {"id": uuid.UUID(resp["id"])},
        )).one()
    assert row.hmac_secret_ref is not None
    assert row.api_key_hash is None
    assert row.trigger_type == "webhook"
    async with session_scope(tenant_id=str(tenant_id)) as s:
        from vibecanvas_api.services.deployment_secret_config import (
            resolve_deployment_hmac_secret,
        )
        from vibecanvas_api.storage.repo_deployments import DeploymentsRepo

        deployment = await DeploymentsRepo(s).get(uuid.UUID(resp["id"]))
        assert await resolve_deployment_hmac_secret(s, deployment) == secret


@pytest.mark.asyncio
async def test_smuggled_tenant_id_is_ignored_at_handler_level(
    pg_engine, app_engine,
):
    """End-to-end G4b: even if a client crafts a body that LOOKED like it
    was for another tenant, the row lands under ``ctx.tenant_id``."""
    from vibecanvas_api.storage.db import session_scope

    real_tenant = uuid.uuid4()
    other_tenant = uuid.uuid4()    # what the attacker tries to claim
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, real_tenant, user_id)
    # Insert the "other" tenant so it actually exists (so an FK to it
    # wouldn't trivially fail for the wrong reason).
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'other')"),
            {"t": other_tenant},
        )
    await _seed_workflow(app_engine, wf_id, real_tenant, user_id)

    ctx = _StubCtx(real_tenant, user_id)
    body = CreateDeploymentBody.model_validate({
        "wf_id": wf_id, "name": "Bot", "slug": "trust-bnd",
        "trigger_type": "api", "version_pin": "head",
        # Smuggled — must be dropped, not honoured.
        "tenant_id": str(other_tenant),
        "user_id": str(uuid.uuid4()),
    })

    async with session_scope(tenant_id=str(real_tenant)) as s:
        resp = await create_deployment(
            body=body, request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    async with pg_engine.connect() as c:
        row = (await c.execute(
            text("SELECT tenant_id, user_id FROM deployments WHERE id = :id"),
            {"id": uuid.UUID(resp["id"])},
        )).one()
    assert row.tenant_id == real_tenant, "tenant_id MUST come from ctx"
    assert row.user_id == user_id, "user_id MUST come from ctx"


@pytest.mark.asyncio
async def test_legacy_major_is_resolved_once_to_a_specific_version(pg_engine, app_engine):
    """A legacy branch selector is stored as a complete fixed version."""
    from vibecanvas_api.storage.db import session_scope

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)
    # Two versions; HEAD is the highest (major, sub) lexicographically.
    await _seed_workflow_version(
        app_engine, wf_id, tenant_id, user_id, major=1, sub=0)
    await _seed_workflow_version(
        app_engine, wf_id, tenant_id, user_id, major=2, sub=3)

    ctx = _StubCtx(tenant_id, user_id)
    body = CreateDeploymentBody.model_validate({
        "wf_id": wf_id, "name": "Pinned", "slug": "pinned-1",
        "trigger_type": "api", "version_pin": "major", "pinned_major": 2,
    })

    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await create_deployment(
            body=body, request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    async with pg_engine.connect() as c:
        row = (await c.execute(
            text(
                "SELECT pinned_major, pinned_sub, version_pin "
                "FROM deployments WHERE id = :id"
            ),
            {"id": uuid.UUID(resp["id"])},
        )).one()
    assert row.version_pin == "specific"
    assert row.pinned_major == 2
    assert row.pinned_sub == 3


@pytest.mark.asyncio
async def test_specific_version_pin_404_when_workflow_is_missing(
    pg_engine, app_engine,
):
    """``version_pin='specific'`` + missing Workflow → non-disclosing 404.

    Strict repository creation always creates encrypted v1.sv0 atomically, so
    a persisted Workflow with no version is no longer a valid application
    state.
    """
    from fastapi import HTTPException

    from vibecanvas_api.storage.db import session_scope

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    # Deliberately do not create the Workflow.

    ctx = _StubCtx(tenant_id, user_id)
    body = CreateDeploymentBody.model_validate({
        "wf_id": wf_id, "name": "Pinned", "slug": "pin-novers",
        "trigger_type": "api", "version_pin": "specific",
    })

    with pytest.raises(HTTPException) as exc_info:
        async with session_scope(tenant_id=str(tenant_id)) as s:
            await create_deployment(
                body=body, request=_StubRequest(), ctx=ctx, session=s,
                service=_DenyAuthz(),
            )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_legacy_head_is_resolved_once(pg_engine, app_engine):
    """``version_pin='head'`` ⇒ ``pinned_major`` / ``pinned_sub`` are
    forced to NULL even if the body supplied them."""
    from vibecanvas_api.storage.db import session_scope

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    await _seed_workflow(app_engine, wf_id, tenant_id, user_id)

    ctx = _StubCtx(tenant_id, user_id)
    body = CreateDeploymentBody.model_validate({
        "wf_id": wf_id, "name": "Pinned", "slug": "head-pin",
        "trigger_type": "api", "version_pin": "head",
        # These should be NULLed out — version_pin='head' overrides.
        "pinned_major": 99, "pinned_sub": 7,
    })

    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await create_deployment(
            body=body, request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    async with pg_engine.connect() as c:
        row = (await c.execute(
            text(
                "SELECT pinned_major, pinned_sub FROM deployments "
                "WHERE id = :id"
            ),
            {"id": uuid.UUID(resp["id"])},
        )).one()
    assert row.pinned_major == 1
    assert row.pinned_sub == 0


@pytest.mark.asyncio
async def test_cross_tenant_slug_collision_409(pg_engine, app_engine):
    """A live public slug owned by another tenant is unavailable."""
    from fastapi import HTTPException

    from vibecanvas_api.storage.db import session_scope

    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    wf_a = f"wf_{uuid.uuid4().hex[:8]}"
    wf_b = f"wf_{uuid.uuid4().hex[:8]}"
    await _seed_tenant_and_user(pg_engine, tenant_a, user_a)
    await _seed_tenant_and_user(pg_engine, tenant_b, user_b)
    await _seed_workflow(app_engine, wf_a, tenant_a, user_a)
    await _seed_workflow(app_engine, wf_b, tenant_b, user_b)

    body_a = CreateDeploymentBody.model_validate({
        "wf_id": wf_a, "name": "Bot A", "slug": "dup-slug",
        "trigger_type": "api", "version_pin": "head",
    })

    async with session_scope(tenant_id=str(tenant_a)) as s:
        await create_deployment(
            body=body_a, request=_StubRequest(),
            ctx=_StubCtx(tenant_a, user_a), session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    body_b = CreateDeploymentBody.model_validate({
        "wf_id": wf_b, "name": "Bot B", "slug": "dup-slug",
        "trigger_type": "api", "version_pin": "head",
    })

    with pytest.raises(HTTPException) as exc_info:
        async with session_scope(tenant_id=str(tenant_b)) as s:
            await create_deployment(
                body=body_b, request=_StubRequest(),
                ctx=_StubCtx(tenant_b, user_b), session=s,
                service=_AllowAuthz(),
            )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_unknown_wf_id_is_non_enumerating_404(pg_engine, app_engine):
    """Unknown workflow is denied before insert without leaking existence."""
    from fastapi import HTTPException

    from vibecanvas_api.storage.db import session_scope

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    # Deliberately do NOT create the workflow.

    ctx = _StubCtx(tenant_id, user_id)
    body = CreateDeploymentBody.model_validate({
        "wf_id": "wf_does_not_exist", "name": "Bot", "slug": "ghost-wf",
        "trigger_type": "api", "version_pin": "head",
    })

    with pytest.raises(HTTPException) as exc_info:
        async with session_scope(tenant_id=str(tenant_id)) as s:
            await create_deployment(
                body=body, request=_StubRequest(), ctx=ctx, session=s,
                service=_DenyAuthz(),
            )
    assert exc_info.value.status_code == 404


# ----------------------------------------------------------- router mount


def test_router_mounted_under_api_v1():
    """The deployments router is registered under ``/api/v1/deployments``."""
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts

    app = build_app()
    paths = {r.path for r in application_route_contexts(app)}
    assert "/api/v1/deployments" in paths, (
        f"POST /api/v1/deployments mount point missing; "
        f"got routes: {sorted(paths)}"
    )
