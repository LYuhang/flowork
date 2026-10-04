"""OrganizationRepo / GroupRepo — tenant-bound AsyncSession repos.

Mirrors ``test_routes_llm_credentials.py``: seed tenant+user via the
RLS-bypassing superuser ``pg_engine``, replay migration 022's backfill so the
personal org + owner membership exist, then exercise the repos through a
``session_scope(tenant_id=...)`` session (sets the ``app.tenant_id`` GUC so RLS
applies).
"""
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_org import GroupRepo, OrganizationRepo
from vibecanvas_api.security.identity_protection import profile_email_lookup_digest

pytestmark = pytest.mark.asyncio


def _backfill_statements():
    path = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / "022_org_permissions_foundation.py")
    spec = importlib.util.spec_from_file_location("_mig022_repo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BACKFILL_SQL


async def _seed_tenant_user_and_backfill(pg_engine):
    t = uuid.uuid4()
    u = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'Acme')"), {"t": t})
        await conn.execute(text(
            "INSERT INTO users(user_id, tenant_id, email) VALUES (:u, :t, :e)"),
            {"u": u, "t": t, "e": f"{u}@x.test"})
        for stmt in _backfill_statements():
            # Current head intentionally removed the legacy grant table.
            # Only replay the retained organization/owner backfill pieces.
            if "resource_grants" in stmt:
                continue
            await conn.execute(text(stmt))
    return t, u


async def test_org_repo_get_returns_personal_org(pg_engine):
    t, _u = await _seed_tenant_user_and_backfill(pg_engine)
    async with session_scope(tenant_id=str(t)) as s:
        org = await OrganizationRepo(s).get_current()
    assert org is not None
    assert org.kind == "personal"


async def test_auth_repo_lists_only_users_organizations(pg_engine):
    t, u = await _seed_tenant_user_and_backfill(pg_engine)
    async with session_scope() as s:
        rows = await AuthRepo(s).list_organizations_for_user(u)
    assert rows == [
        {
            "organization_id": str(t),
            "kind": "personal",
            "slug": f"org-{str(t).replace('-', '')}",
            "name": "Acme",
            "membership_id": rows[0]["membership_id"],
            "role": "owner",
            "status": "active",
        }
    ]


async def test_group_repo_creates_generic_group(pg_engine):
    t, u = await _seed_tenant_user_and_backfill(pg_engine)
    async with session_scope(tenant_id=str(t)) as s:
        group = await GroupRepo(s).create(
            organization_id=t,
            created_by=u,
            name="Engineering",
            kind="department",
            parent_group_id=None,
        )
        group_id = group.group_id
    async with session_scope(tenant_id=str(t)) as s:
        stored = await GroupRepo(s).get(group_id)
    assert stored is not None
    assert stored.kind == "department"


async def test_add_registered_member_preserves_home_and_defaults_to_member(pg_engine):
    company, owner = await _seed_tenant_user_and_backfill(pg_engine)
    home, recipient = await _seed_tenant_user_and_backfill(pg_engine)
    email = f"{recipient}@x.test"
    async with pg_engine.begin() as conn:
        await conn.execute(text("UPDATE organizations SET kind='business' WHERE tenant_id=:t"), {"t": company})
        await conn.execute(text("UPDATE users SET profile_email_lookup_hash=:h WHERE user_id=:u"),
                           {"h": profile_email_lookup_digest(email), "u": recipient})
    async with session_scope(tenant_id=str(company)) as session:
        member = await OrganizationRepo(session).add_registered_member(email=email.upper(), invited_by=owner)
        assert member.user_id == recipient
        assert member.tenant_id == company
        assert member.org_role == "member"
        assert member.status == "active"
        assert member.source == "native"
    async with session_scope(tenant_id=str(company)) as session:
        with pytest.raises(ValueError, match="organization_membership_already_exists"):
            await OrganizationRepo(session).add_registered_member(email=email, invited_by=owner)
    async with pg_engine.connect() as conn:
        assert (await conn.execute(text("SELECT tenant_id FROM users WHERE user_id=:u"), {"u": recipient})).scalar_one() == home
        assert (await conn.execute(text("SELECT org_role FROM org_memberships WHERE tenant_id=:t AND user_id=:u"), {"t": home, "u": recipient})).scalar_one() == "owner"


async def test_add_registered_member_rejects_personal_space(pg_engine):
    personal, owner = await _seed_tenant_user_and_backfill(pg_engine)
    async with session_scope(tenant_id=str(personal)) as session:
        with pytest.raises(ValueError, match="business_organization_required"):
            await OrganizationRepo(session).add_registered_member(email=f"{owner}@x.test", invited_by=owner)


async def test_add_registered_member_rejects_unknown_email(pg_engine):
    company, owner = await _seed_tenant_user_and_backfill(pg_engine)
    async with pg_engine.begin() as conn:
        await conn.execute(text("UPDATE organizations SET kind='business' WHERE tenant_id=:t"), {"t": company})
    async with session_scope(tenant_id=str(company)) as session:
        with pytest.raises(ValueError, match="registered_user_not_found"):
            await OrganizationRepo(session).add_registered_member(email=f"missing-{uuid.uuid4()}@x.test", invited_by=owner)


async def test_concurrent_owner_removal_keeps_one_active_owner(pg_engine):
    import asyncio
    company, owner = await _seed_tenant_user_and_backfill(pg_engine)
    _, second_owner = await _seed_tenant_user_and_backfill(pg_engine)
    async with pg_engine.begin() as conn:
        await conn.execute(text("UPDATE organizations SET kind='business' WHERE tenant_id=:t"), {"t": company})
        await conn.execute(text("INSERT INTO org_memberships(membership_id, tenant_id, user_id, org_role, status, source) VALUES (:m, :t, :u, 'owner', 'active', 'native')"), {"m": uuid.uuid4(), "t": company, "u": second_owner})

    ready = asyncio.Event()
    count = 0

    async def remove(user_id):
        nonlocal count
        try:
            async with session_scope(tenant_id=str(company)) as session:
                repo = OrganizationRepo(session)
                membership = await repo.get_member(user_id)
                count += 1
                if count == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=10)
                await repo.update_member(membership, role='member', status='active')
            return 'removed'
        except ValueError as exc:
            return str(exc)

    results = await asyncio.wait_for(asyncio.gather(remove(owner), remove(second_owner)), timeout=20)
    assert sorted(results) == ['organization_requires_active_owner', 'removed']
    async with pg_engine.connect() as conn:
        remaining = (await conn.execute(text("SELECT count(*) FROM org_memberships WHERE tenant_id=:t AND org_role='owner' AND status='active'"), {"t": company})).scalar_one()
        assert remaining == 1
