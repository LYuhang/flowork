"""Deployments T15 — G1-G14 verification gates.

Each gate is one or more pytest functions covering a §12 spec invariant.
Infrastructure-bound gates are skipped with explicit rationale; they run
in staging where Redis + a DBOS worker + a real beat are available.

Strategy mirrors T1-T13: seed via ``pg_engine``/``app_engine`` (superuser
+ app-role respectively), drive route handlers directly (no HTTP
roundtrip), and mock external infrastructure (Redis and background enqueue)
at the call site.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.authorization.types import Decision
from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


@pytest.fixture
def _sandbox_fs_store(monkeypatch):
    """The deployment invoke path (``invoke_sync`` / ``test_invoke``) is now
    sandbox-only (P2 cutover): it materializes a per-run ``/run`` dir from the
    object store and bind-mounts it into gVisor. The in-memory store cannot be
    bind-mounted, so a real (filesystem) object store is required. Mirrors
    ``test_execution_e2e_pg._sandbox_oneshot_fs``."""
    from vibecanvas_api.config import config as _cfg
    from vibecanvas_api.services.sandbox import _gvisor_runnable

    if not _gvisor_runnable():
        pytest.skip("full rootless gVisor profile is unavailable")
    monkeypatch.setattr(_cfg.object_store, "provider", "filesystem",
                        raising=False)
    monkeypatch.setattr(_cfg.object_store, "fs_root",
                        tempfile.mkdtemp(prefix="vc-os-"), raising=False)
    monkeypatch.setattr(
        _cfg,
        "kms_provider",
        "local",
        raising=False,
    )
    monkeypatch.setattr(
        _cfg,
        "kms_local_master_key",
        base64.urlsafe_b64encode(b"deployment-gate-kms-key-material"[:32]).decode(),
        raising=False,
    )


# --- Minimal workflow content used by tests that need a real execution path.
# Mirrors the shape used by T6/T12 — StartNode -> EndNode echoing 'x'.
_MIN_WF = {
    "node_1": {
        "node_id": "node_1", "node_type": "StartNode", "node_name": "__start__",
        "node_description": "",
        "input_fields": {"x": {"type": "int", "value": 0, "reference": None}},
        "output_fields": {"x": {"type": "int", "description": ""}},
        "node_config": {}, "children": ["node_2"], "__attributes__": {"x": 0, "y": 0},
    },
    "node_2": {
        "node_id": "node_2", "node_type": "EndNode", "node_name": "__end__",
        "node_description": "",
        "input_fields": {"y": {"type": "int", "value": 0, "reference": "__start__.x"}},
        "output_fields": {}, "node_config": {}, "children": [],
        "__attributes__": {"x": 200, "y": 0},
    },
    "__meta__": {"workflow_name": "min", "workflow_description": ""},
}


async def _seed_full(
    pg_engine, app_engine, *,
    trigger="api", qps=10,
    api_key_plain=None,
    version_pin="specific", pinned_major=1, pinned_sub=0,
    hmac_secret=None,
):
    """Returns (tenant_id, user_id, wf_id, dep_id, slug, api_key_plain | None, hmac_secret | None)."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    dep_id = uuid.uuid4()
    slug = f"g15-{uuid.uuid4().hex[:6]}"
    plain = api_key_plain or (
        f"vc_g15_{uuid.uuid4().hex[:12]}" if trigger == "api" else None
    )
    secret = hmac_secret if hmac_secret is not None else (
        f"whsec_g15_{uuid.uuid4().hex[:24]}" if trigger == "webhook" else None
    )
    api_hash = hashlib.sha256(plain.encode()).hexdigest() if plain else None

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
             "e": f"g-{uuid.uuid4().hex[:6]}@example.com"},
        )
        await c.execute(
            text(
                "INSERT INTO organizations("
                "tenant_id, kind, slug, name, created_by"
                ") VALUES (:t, 'personal', :slug, 'Test account', :u)"
            ),
            {"t": tenant_id, "u": user_id, "slug": f"test-{tenant_id.hex}"},
        )
    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=wf_id,
            name="min",
            creator_user_id=str(user_id),
            initial_workflow=_MIN_WF,
        )
        if (pinned_major, pinned_sub) != (1, 0):
            raise ValueError("strict deployment test seed only supports v1.sv0")
        secret_ref = None
        if secret:
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
            name="G",
            slug=slug,
            trigger_type=trigger,
            version_pin=version_pin,
            pinned_major=pinned_major if version_pin == "specific" else None,
            pinned_sub=pinned_sub if version_pin == "specific" else None,
            api_key_hash=api_hash,
            hmac_secret_ref=secret_ref,
            hmac_secret_version=1,
            rate_limit_qps=qps,
        )
    return tenant_id, user_id, wf_id, dep_id, slug, plain, secret


class _Ctx:
    """Stand-in AuthContext when calling handlers directly. The real
    AuthContext stores tenant_id / user_id as ``str`` — matched here so
    handlers that ``uuid.UUID(ctx.tenant_id)`` (create_deployment) work."""

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
        self.email = "g15@example.com"


class _StubRequest:
    def __init__(self):
        self.headers = {}
        self.client = None
        self.state = SimpleNamespace(request_id="test-request")
        self.app = SimpleNamespace(
            state=SimpleNamespace(openfga_client=None),
        )


class _AllowAuthz:
    async def check(self, *args, **kwargs):
        return Decision(allowed=True, reason_code="test_fixture")

    async def list_authorized_ids(self, *args, **kwargs):
        return ()

    async def batch_check(self, checks):
        return tuple(
            Decision(allowed=True, reason_code="test_fixture")
            for _ in checks
        )


# ---------- G1: create returns plaintext exactly once ----------


@pytest.mark.asyncio
async def test_g1_create_returns_plaintext_once(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    """Spec G1 — POST /deployments returns plaintext credential;
    subsequent GET does NOT."""
    from vibecanvas_api.routes.deployments import (
        create_deployment, get_deployment, CreateDeploymentBody,
    )
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
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
             "e": f"g1-{uuid.uuid4().hex[:6]}@example.com"},
        )
        await c.execute(
            text(
                "INSERT INTO organizations("
                "tenant_id, kind, slug, name, created_by"
                ") VALUES (:t, 'personal', :slug, 'Test account', :u)"
            ),
            {"t": tenant_id, "u": user_id, "slug": f"test-{tenant_id.hex}"},
        )
    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=wf_id,
            name="G1",
            creator_user_id=str(user_id),
        )

    body = CreateDeploymentBody.model_validate({
        "wf_id": wf_id, "name": "G1 API",
        "slug": f"g1-{uuid.uuid4().hex[:6]}",
        "trigger_type": "api", "version_pin": "head",
    })
    ctx = _Ctx(tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await create_deployment(
            body=body, request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    assert resp["api_key"].startswith("vc_")
    dep_id_str = resp["id"]

    async with session_scope(tenant_id=str(tenant_id)) as s:
        get_resp = await get_deployment(
            dep_id=uuid.UUID(dep_id_str), request=_StubRequest(), ctx=ctx,
            session=s, service=_AllowAuthz(),
        )
    assert "api_key" not in get_resp, "Plaintext leaked on subsequent GET"
    assert "api_key_hash" not in get_resp, "Hash leaked on GET"


# ---------- G2: sync invoke returns 200 ----------


@pytest.mark.asyncio
async def test_g2_invoke_short_returns_outputs(
    pg_engine, app_engine, monkeypatch, pg_url, _sandbox_fs_store,
):
    """Spec G2 — happy path sync invoke returns outputs + exec_time_ms."""
    from vibecanvas_api.routes.deployment_invoke import invoke_sync
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    _, _, _, _, slug, key, _ = await _seed_full(
        pg_engine, app_engine, trigger="api",
    )
    resp = await invoke_sync(
        slug=slug, body={"x": 5}, authorization=f"Bearer {key}",
    )
    if not isinstance(resp, dict):
        pytest.fail(resp.body.decode("utf-8"))
    assert "outputs" in resp
    assert "exec_time_ms" in resp


# ---------- G3: long workflow gateway timeout — staging only ----------


@pytest.mark.skip(
    reason=(
        "G3 — needs a long-running workflow + edge gateway timeout config; "
        "staging only."
    )
)
def test_g3_long_workflow_504():
    pass


# ---------- G4: async submit returns 202 + task_id ----------


@pytest.mark.asyncio
async def test_g4_runs_async_returns_task_id(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    """Spec G4 — POST /runs enqueues a DBOS task and returns task_id."""
    from vibecanvas_api.routes.deployment_invoke import invoke_async
    from vibecanvas_api.services import deployments_service
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)
    monkeypatch.setattr(
        deployments_service, "enqueue_background_job_in_transaction", AsyncMock(),
    )

    _, _, _, _, slug, key, _ = await _seed_full(
        pg_engine, app_engine, trigger="api",
    )
    resp = await invoke_async(
        slug=slug, body={"x": 1}, authorization=f"Bearer {key}",
    )
    assert "task_id" in resp


# ---------- G5: webhook signature good/bad branches ----------


@pytest.mark.asyncio
async def test_g5_webhook_signature_branches(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    """Spec G5 — bad HMAC → 401; good HMAC → 202 + task_id."""
    from vibecanvas_api.routes.deployment_invoke import webhook
    from vibecanvas_api.services import deployments_service
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)
    monkeypatch.setattr(
        deployments_service, "enqueue_background_job_in_transaction", AsyncMock(),
    )

    _, _, _, _, slug, _, secret = await _seed_full(
        pg_engine, app_engine, trigger="webhook",
    )

    class _Req:
        def __init__(self, headers, body):
            self.headers = headers
            self._body = body

        async def body(self):
            return self._body

    body = b"{}"
    ts = str(int(time.time()))
    bad = _Req(
        {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Vibecanvas-Signature": "sha256=" + "0" * 64,
            "X-Vibecanvas-Timestamp": ts,
        },
        body,
    )
    with pytest.raises(HTTPException) as exc:
        await webhook(slug=slug, request=bad)
    assert exc.value.status_code == 401

    good_sig = "sha256=" + hmac.new(
        secret.encode(), ts.encode() + b"." + body, "sha256",
    ).hexdigest()
    good = _Req(
        {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Vibecanvas-Signature": good_sig,
            "X-Vibecanvas-Timestamp": ts,
        },
        body,
    )
    resp = await webhook(slug=slug, request=good)
    assert "task_id" in resp


# ---------- G7: RLS isolates tenants ----------


@pytest.mark.asyncio
async def test_g7_rls_isolates_tenants(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    """Spec G7 — tenant B's list does NOT include tenant A's deployment."""
    from vibecanvas_api.routes.deployments import list_deployments
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    _, _, _, dep_a_id, _, _, _ = await _seed_full(
        pg_engine, app_engine, trigger="api",
    )

    # Seed tenant B (just tenant + user; no need for workflow/deployment).
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
            {"u": user_b, "t": tenant_b,
             "e": f"g7-{uuid.uuid4().hex[:6]}@example.com"},
        )

    ctx_b = _Ctx(tenant_b, user_b)
    async with session_scope(tenant_id=str(tenant_b)) as s:
        resp = await list_deployments(
            request=_StubRequest(),
            trigger_type=None, enabled=None, workflow_id=None,
            limit=50, offset=0, ctx=ctx_b, session=s,
            service=_AllowAuthz(),
        )
    assert all(str(d["id"]) != str(dep_a_id) for d in resp["items"]), (
        "RLS violation — tenant B saw tenant A's deployment"
    )


# ---------- G8: rate limit returns 429 ----------


@pytest.mark.asyncio
async def test_g8_rate_limit_returns_429(monkeypatch):
    """Spec G8 — check_rate_limit raises 429 on overflow (mocked Redis).

    Live Redis test is staging-only; the unit-level guarantee is that the
    Lua INCR count > qps triggers the 429 with a Retry-After header.
    """
    from vibecanvas_api.services import rate_limit
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


# ---------- G9: cluster_role split — staging only ----------


@pytest.mark.skip(
    reason=(
        "G9 — needs two-process control/data plane split; staging only."
    )
)
def test_g9_cluster_role_split():
    pass


# ---------- G10: metrics are deployment-owned, not task-backed ----------


@pytest.mark.asyncio
async def test_g10_metrics_aggregates_history(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    """Deployment metrics no longer aggregate global Task rows."""
    from vibecanvas_api.routes.deployments import metrics
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    tenant_id, user_id, _, dep_id, _, _, _ = await _seed_full(
        pg_engine, app_engine, trigger="api",
    )
    base = datetime.now(timezone.utc) - timedelta(hours=10)

    ctx = _Ctx(tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await metrics(
            dep_id=dep_id,
            request=_StubRequest(),
            from_=base - timedelta(hours=1),
            to=base + timedelta(hours=20),
            bucket="hour",
            ctx=ctx, session=s, service=_AllowAuthz(),
        )
    assert resp["series"] == []


# ---------- G11: workflow delete blocked when enabled deployment exists -----


@pytest.mark.asyncio
async def test_g11_workflow_delete_blocked(pg_engine, app_engine):
    """Spec G11 — the app-layer guard SQL catches enabled deployments
    referencing a workflow (T5 wf-delete guard)."""
    tenant_id, _, wf_id, _, _, _, _ = await _seed_full(
        pg_engine, app_engine, trigger="api",
    )
    async with app_engine.connect() as c:
        await c.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(tenant_id)},
        )
        # Mirror the guard SQL from T5's delete_workflow.
        in_use = (await c.execute(
            text(
                "SELECT 1 FROM deployments "
                "WHERE wf_id = :wf AND enabled = TRUE "
                "AND deleted_at IS NULL LIMIT 1"
            ),
            {"wf": wf_id},
        )).one_or_none()
    assert in_use is not None, "Guard should match the enabled deployment"


# ---------- G12: version pin specific freezes the version ----------


@pytest.mark.asyncio
async def test_g12_version_pin_specific_freezes(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    """Spec G12 — a deployment pinned to (1,0) loads (1,0) even when
    (2,0) exists. load_workflow_version honours the pin (not HEAD)."""
    from vibecanvas_api.services.workflow_runner import load_workflow_version
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    tenant_id, user_id, wf_id, dep_id, _, _, _ = await _seed_full(
        pg_engine, app_engine, trigger="api",
        version_pin="specific", pinned_major=1, pinned_sub=0,
    )
    # Seed a v2.0 with different content.
    async with session_scope(tenant_id=str(tenant_id)) as session:
        v2 = dict(_MIN_WF)
        v2["__meta__"] = {"workflow_name": "v2", "workflow_description": ""}
        await WorkflowRepo(session, str(user_id)).new_version(
            wf_id, v2, note="v2"
        )

    dep_row = {
        "id": dep_id, "tenant_id": tenant_id, "wf_id": wf_id,
        "version_pin": "specific", "pinned_major": 1, "pinned_sub": 0,
    }
    loaded = await load_workflow_version(dep_row)
    # v1.0 has workflow_name="min"; v2.0 has "v2". We pinned to v1.
    assert loaded["__meta__"]["workflow_name"] == "min", (
        f"Version pin not honored; got {loaded['__meta__']}"
    )


# ---------- G13: test-invoke with user session ----------


@pytest.mark.asyncio
async def test_g13_test_invoke_via_user_session(
    pg_engine, app_engine, monkeypatch, pg_url, _sandbox_fs_store,
):
    """Spec G13 — POST /<id>/test-invoke runs under user session auth
    (NOT api_key) and returns outputs."""
    from vibecanvas_api.routes.deployments import test_invoke
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    tenant_id, user_id, _, dep_id, _, _, _ = await _seed_full(
        pg_engine, app_engine, trigger="api",
    )
    ctx = _Ctx(tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await test_invoke(
            dep_id=dep_id, body={"x": 3}, request=_StubRequest(), ctx=ctx,
            session=s, service=_AllowAuthz(),
        )
    assert "outputs" in resp


# ---------- G14: rotate-key invalidates old key ----------


@pytest.mark.asyncio
async def test_g14_rotate_key_invalidates_old(
    pg_engine, app_engine, monkeypatch, pg_url,
):
    """Spec G14 — POST /rotate-key returns a fresh plaintext AND
    invalidates the previous plaintext against the resolver."""
    from vibecanvas_api.routes.deployments import rotate_key
    from vibecanvas_api.services.deployments_service import (
        resolve_deployment_and_bind_tenant,
    )
    from vibecanvas_api.services.tenant_db import tenant_id_var
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)

    tenant_id, user_id, _, dep_id, _, old_key, _ = await _seed_full(
        pg_engine, app_engine, trigger="api",
    )

    # Old key resolves before rotation.
    tenant_id_var.set(None)
    pre = await resolve_deployment_and_bind_tenant(api_key=old_key)
    assert pre is not None
    assert pre["id"] == dep_id

    # Rotate.
    ctx = _Ctx(tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        resp = await rotate_key(
            dep_id=dep_id, request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()
    new_key = resp["api_key"]
    assert new_key.startswith("vc_") and new_key != old_key

    # Old key MUST stop matching; new key MUST match.
    tenant_id_var.set(None)
    assert (await resolve_deployment_and_bind_tenant(api_key=old_key)) is None
    tenant_id_var.set(None)
    matched = await resolve_deployment_and_bind_tenant(api_key=new_key)
    assert matched is not None
    assert matched["id"] == dep_id
