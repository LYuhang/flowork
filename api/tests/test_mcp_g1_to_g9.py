"""MCP T9 — G1-G9 verification gates (plan §1746-1907).

These are the final ship-gate tests for the MCP Client integration. They
exercise the production code paths end-to-end at the seam where each
contract is the most fragile:

* RLS cross-tenant isolation (G3)
* tool-name namespacing (G4)
* dry-run probe contract (G6)
* enable/disable + soft-delete semantics (G7, G8)

G1 / G2 / G9 require a real local MCP server fixture (mcp-server-
everything or similar) which the sandbox cannot spawn — they are
explicitly skipped here and will be exercised in staging.

Strategy mirrors MCP T5 / T6: handler-direct-call + ``_StubCtx`` over a
real RLS-bound DB session; ``handshake_one`` is mocked so no real
network traffic is required.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from vibecanvas_api.authorization.types import Decision
from vibecanvas_api.routes.mcp_servers import (
    CreateBody,
    create_mcp_server,
    delete_mcp_server,
    dry_run_handshake,
    list_mcp_servers,
    patch_mcp_server,
    PatchBody,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo


# --------------------------------------------------------------------- helpers


async def _seed_tenant_and_user(pg_engine, tenant_id, user_id) -> None:
    """Insert tenant + user via the RLS-bypassing superuser engine.
    Auth tables are RLS-free so a plain begin() block is fine.
    Copied inline (not extracted) to keep this gate file self-contained,
    mirroring the per-file convention in T2/T5/T6 tests."""
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
             "e": f"mcp-g-{uuid.uuid4().hex[:6]}@example.com"},
        )
        await c.execute(
            text(
                "INSERT INTO organizations("
                "tenant_id, kind, slug, name, created_by"
                ") VALUES (:t, 'personal', :slug, 'Test account', :u)"
            ),
            {"t": tenant_id, "u": user_id, "slug": f"test-{tenant_id.hex}"},
        )


class _StubCtx:
    """Lightweight stand-in for ``AuthContext`` (handlers only read
    ``tenant_id`` / ``user_id``)."""

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
        self.app = SimpleNamespace(state=SimpleNamespace(openfga_client=None))


class _AllowAuthz:
    def __init__(self, resource_ids=()):
        self._resource_ids = tuple(str(value) for value in resource_ids)

    async def check(self, *args, **kwargs):
        return Decision(allowed=True, reason_code="test_fixture")

    async def list_authorized_ids(self, *args, **kwargs):
        return self._resource_ids

    async def batch_check(self, checks):
        return tuple(
            Decision(allowed=True, reason_code="test_fixture")
            for _ in checks
        )


def _body(**overrides) -> CreateBody:
    """Well-formed default ``CreateBody`` — override per-test."""
    base = {
        "name": "Notion",
        "tool_prefix": "notion",
        "transport": "sse",
        "endpoint": "https://example.com/sse",
        "auth_config": {"type": "bearer", "token": "tok-g-test"},
    }
    base.update(overrides)
    return CreateBody.model_validate(base)


def _fake_ok(tool_count: int, tool_names: list[dict]) -> dict:
    """``handshake_one`` success shape."""
    return {
        "status": "ok",
        "tool_count": tool_count,
        "tool_names": tool_names,
        "tools": [],
    }


# ====================================================================
# G1 — SSE transport + bearer + agent uses tool (staging-only)
# ====================================================================


@pytest.mark.skip(reason=(
    "G1 needs a real local MCP server (e.g. mcp-server-everything) "
    "the sandbox cannot spawn — exercise in staging."
))
def test_g1_sse_bearer_agent_uses_tool():
    """Spec: connect to a real SSE MCP server with bearer auth, build the
    agent for the tenant, and confirm the agent can invoke a tool from
    that server. Validated in staging with the mcp-server-everything
    fixture."""


# ====================================================================
# G2 — streamable_http transport parallel of G1 (staging-only)
# ====================================================================


@pytest.mark.skip(reason=(
    "G2 needs a real local MCP server speaking streamable_http — "
    "exercise in staging."
))
def test_g2_streamable_http_bearer_agent_uses_tool():
    """Spec: same as G1 but with ``transport='streamable_http'``. Validates
    that the loader passes the transport through to MultiServerMCPClient
    correctly. Staging-only."""


# ====================================================================
# G3 — cross-tenant RLS isolation
# ====================================================================


@pytest.mark.asyncio
async def test_g3_cross_tenant_rls_isolation(pg_engine):
    """Two tenants each seed a server. Tenant B's list_mcp_servers must
    NOT include tenant A's row — FORCE RLS on mcp_servers binds even the
    table owner. This is the primary multi-tenancy invariant: a tenant
    seeing another tenant's MCP endpoint URL would leak which third-party
    services they integrate with."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_a, user_a)
    await _seed_tenant_and_user(pg_engine, tenant_b, user_b)

    ctx_a = _StubCtx(tenant_a, user_a)
    ctx_b = _StubCtx(tenant_b, user_b)

    with patch(
        "vibecanvas_api.routes.mcp_servers.handshake_one",
        new_callable=AsyncMock,
    ) as mock_hs:
        mock_hs.return_value = _fake_ok(
            tool_count=1,
            tool_names=[{"name": "secret_tool", "description": "x"}],
        )

        # Tenant A seeds its server.
        async with session_scope(tenant_id=str(tenant_a)) as s:
            a_resp = await create_mcp_server(
                body=_body(name="A-only", tool_prefix="aonly"),
                request=_StubRequest(), ctx=ctx_a, session=s,
                service=_AllowAuthz(),
            )
            await s.commit()

        # Tenant B seeds a server with a DIFFERENT prefix — proves the
        # listing is filtered by tenant, not by some other accident.
        async with session_scope(tenant_id=str(tenant_b)) as s:
            b_resp = await create_mcp_server(
                body=_body(name="B-only", tool_prefix="bonly"),
                request=_StubRequest(), ctx=ctx_b, session=s,
                service=_AllowAuthz(),
            )
            await s.commit()

    # Tenant B's listing — must contain ONLY B's row.
    async with session_scope(tenant_id=str(tenant_b)) as s:
        b_list = await list_mcp_servers(
            request=_StubRequest(), ctx=ctx_b, session=s,
            service=_AllowAuthz((b_resp["id"],)),
        )

    b_ids = {item["id"] for item in b_list["items"]}
    assert a_resp["id"] not in b_ids, (
        f"RLS leak — tenant B saw tenant A's mcp_server id "
        f"{a_resp['id']}"
    )
    assert b_resp["id"] in b_ids

    # Symmetric — tenant A's listing must contain ONLY A's row.
    async with session_scope(tenant_id=str(tenant_a)) as s:
        a_list = await list_mcp_servers(
            request=_StubRequest(), ctx=ctx_a, session=s,
            service=_AllowAuthz((a_resp["id"],)),
        )

    a_ids = {item["id"] for item in a_list["items"]}
    assert b_resp["id"] not in a_ids
    assert a_resp["id"] in a_ids


# ====================================================================
# G4 — tool-name namespacing (built-in names unaffected)
# ====================================================================


@pytest.mark.asyncio
async def test_g4_tool_name_namespacing_preserves_builtins(pg_engine):
    """Namespaced remote tools do not replace Workflow SubAgent built-ins.

    File operations now use the built-in bash tool; read_file and get_workflow
    are retired built-ins. A remote get_workflow remains independently usable
    as notion__get_workflow without changing the built-in tool registry.
    """
    from vibecanvas_api.agents.tools import builtin_tool_names

    # Built-in remains present and identifiable by bare name (no ``__``).
    builtin_names = builtin_tool_names()
    assert "bash" in builtin_names, (
        "built-in bash must remain in TOOLS regardless of MCP "
        "third-party tools"
    )
    # And no built-in name itself contains ``__`` — the invariant the
    # bare-name collision check relies on.
    assert not any("__" in n for n in builtin_names), (
        f"built-in names must not contain '__': "
        f"{[n for n in builtin_names if '__' in n]}"
    )

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    # MCP server exposes a tool named "get_workflow" — the prefixed form
    # ``notion__get_workflow`` does NOT collide with any built-in.
    with patch(
        "vibecanvas_api.routes.mcp_servers.handshake_one",
        new_callable=AsyncMock,
    ) as mock_hs:
        mock_hs.return_value = _fake_ok(
            tool_count=1,
            tool_names=[
                {"name": "get_workflow", "description": "third-party"},
            ],
        )
        async with session_scope(tenant_id=str(tenant_id)) as s:
            resp = await create_mcp_server(
                body=_body(name="Notion", tool_prefix="notion"),
                request=_StubRequest(), ctx=ctx, session=s,
                service=_AllowAuthz(),
            )
            await s.commit()

    # Create succeeded — the namespacing means no bare-name collision.
    assert resp["last_handshake_status"] == "ok"
    assert resp["tool_prefix"] == "notion"
    assert builtin_tool_names() == builtin_names


# ====================================================================
# G6 — POST /test dry-run probe
# ====================================================================


@pytest.mark.asyncio
async def test_g6_dry_run_returns_probe_shape(pg_engine):
    """``POST /mcp-servers/test`` is a pure probe — no DB write. With
    handshake mocked to ``ok+2`` tools, the response shape is:
    ``{"ok": True, "tool_count": 2, "tool_names": [...]}``. The Add
    wizard's frontend depends on this exact shape (T8)."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    with patch(
        "vibecanvas_api.routes.mcp_servers.handshake_one",
        new_callable=AsyncMock,
    ) as mock_hs:
        mock_hs.return_value = _fake_ok(
            tool_count=2,
            tool_names=[
                {"name": "a", "description": "a"},
                {"name": "b", "description": "b"},
            ],
        )
        resp = await dry_run_handshake(
            body=_body(), request=_StubRequest(), ctx=ctx,
            service=_AllowAuthz(),
        )

    assert resp == {
        "ok": True,
        "tool_count": 2,
        "tool_names": [
            {"name": "a", "description": "a"},
            {"name": "b", "description": "b"},
        ],
    }


# ====================================================================
# G7 — disable removes tools next turn
# ====================================================================


@pytest.mark.asyncio
async def test_g7_disable_excludes_from_loader_next_turn(pg_engine):
    """PATCH ``{"enabled": False}`` flips the row's ``enabled`` flag, and
    ``McpServersRepo.list_enabled()`` (called by the loader on every
    agent turn) immediately stops yielding it. No restart, no cache
    invalidation needed — the next agent turn simply does not include
    that server's tools."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    # Seed via the public create endpoint so we know the row is in the
    # exact state the production code writes.
    with patch(
        "vibecanvas_api.routes.mcp_servers.handshake_one",
        new_callable=AsyncMock,
    ) as mock_hs:
        mock_hs.return_value = _fake_ok(
            tool_count=1,
            tool_names=[{"name": "ping", "description": "p"}],
        )
        async with session_scope(tenant_id=str(tenant_id)) as s:
            resp = await create_mcp_server(
                body=_body(name="Toggle", tool_prefix="toggle"),
                request=_StubRequest(), ctx=ctx, session=s,
                service=_AllowAuthz(),
            )
            await s.commit()

    sid = uuid.UUID(resp["id"])

    # Sanity — before disable, list_enabled yields it.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = McpServersRepo(s)
        rows_before = await repo.list_enabled()
    assert any(r["id"] == sid for r in rows_before), (
        "list_enabled must yield the row before disable"
    )

    # PATCH enabled=False.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        await patch_mcp_server(
            server_id=sid,
            body=PatchBody.model_validate({"enabled": False}),
            request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    # After disable — list_enabled must NOT yield it.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = McpServersRepo(s)
        rows_after = await repo.list_enabled()
    assert not any(r["id"] == sid for r in rows_after), (
        f"list_enabled must exclude disabled row {sid}, but it still "
        f"appeared in {[r['id'] for r in rows_after]}"
    )


# ====================================================================
# G8 — soft-delete + recreate (same name + same prefix)
# ====================================================================


@pytest.mark.asyncio
async def test_g8_soft_delete_then_recreate_same_name_and_prefix(pg_engine):
    """After DELETE, recreating with the SAME ``name`` + SAME
    ``tool_prefix`` succeeds — the partial UNIQUE indexes are
    ``WHERE deleted_at IS NULL`` so the soft-deleted row does not
    block re-use. Critical for the "I made a mistake, let me re-add"
    UX path."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    with patch(
        "vibecanvas_api.routes.mcp_servers.handshake_one",
        new_callable=AsyncMock,
    ) as mock_hs:
        mock_hs.return_value = _fake_ok(
            tool_count=1,
            tool_names=[{"name": "x", "description": "x"}],
        )

        # First create.
        async with session_scope(tenant_id=str(tenant_id)) as s:
            first = await create_mcp_server(
                body=_body(name="Recyclable", tool_prefix="recycle"),
                request=_StubRequest(), ctx=ctx, session=s,
                service=_AllowAuthz(),
            )
            await s.commit()
        first_id = uuid.UUID(first["id"])

        # Delete.
        async with session_scope(tenant_id=str(tenant_id)) as s:
            await delete_mcp_server(
                server_id=first_id, request=_StubRequest(), ctx=ctx,
                session=s, service=_AllowAuthz(),
            )
            await s.commit()

        # Recreate with the SAME name + prefix — must NOT collide with
        # the soft-deleted row (partial UNIQUE WHERE deleted_at IS NULL).
        async with session_scope(tenant_id=str(tenant_id)) as s:
            second = await create_mcp_server(
                body=_body(name="Recyclable", tool_prefix="recycle"),
                request=_StubRequest(), ctx=ctx, session=s,
                service=_AllowAuthz(),
            )
            await s.commit()

    assert second["tool_prefix"] == "recycle"
    assert second["name"] == "Recyclable"
    assert uuid.UUID(second["id"]) != first_id, (
        "recreate must mint a new id, not reuse the soft-deleted row"
    )

    # Sanity: only the new row is live + listed.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        listing = await list_mcp_servers(
            request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz((second["id"],)),
        )
    live_ids = {item["id"] for item in listing["items"]}
    assert second["id"] in live_ids
    assert first["id"] not in live_ids


# ====================================================================
# G9 — E2E agent uses an MCP add tool (staging-only)
# ====================================================================


@pytest.mark.skip(reason=(
    "G9 needs a real local MCP server fixture for the agent to call. "
    "Exercise in staging with mcp-server-everything's `add` tool."
))
def test_g9_agent_can_call_mcp_add_tool():
    """Spec: with a real MCP server providing an ``add(a, b)`` tool
    registered for the tenant, the agent picks it up and returns the
    correct sum on a "compute 2+3" prompt. Staging-only."""
