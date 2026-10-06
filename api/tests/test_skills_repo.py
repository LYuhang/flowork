"""Skills T1 — ``SkillsRepo`` CRUD + bundle blobs + soft-delete.

We drive the repo through ``session_scope(tenant_id=...)`` so it runs against
the production-shape app role + RLS GUC, exactly as a route handler would
(mirrors ``test_mcp_servers_repo.py``). Tenant + user are seeded through the
RLS-bypassing superuser ``pg_engine``; bundle blobs land in the configured
ObjectStore (the in-memory test default).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_skills import SkillsRepo


async def _seed_tenant_and_user(pg_engine, tenant_id, user_id) -> None:
    """Insert tenant + user via the RLS-bypassing superuser engine.
    Auth tables are RLS-free so a plain begin() block is fine."""
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
             "e": f"skills-repo-{uuid.uuid4().hex[:6]}@example.com"},
        )


@pytest.mark.asyncio
async def test_insert_list_read_softdelete(pg_engine):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)

    published_body = (
        b"---\nname: greet\ndescription: say hi\n---\n"
        b"published-secret-marker-71d04cd8"
    )
    draft_body = b"draft-secret-marker-55c9b406"

    async with session_scope(tenant_id=str(tenant_id), user_id=str(user_id)) as s:
        repo = SkillsRepo(s)
        sid = await repo.insert(
            tenant_id=str(tenant_id), user_id=str(user_id),
            name="greet", description="say hi",
            version=1, allowed_tools=["canvas", "mcp:x"], source="custom",
            files=[(
                "SKILL.md", "text/markdown",
                published_body,
            )],
        )
        await repo.save_draft(
            skill_id=sid,
            tenant_id=tenant_id,
            files=[("draft.txt", "text/plain", draft_body)],
        )
        await s.commit()

    async with pg_engine.connect() as c:
        revision_ciphertext = (await c.execute(
            text(
                "SELECT content_ciphertext FROM skill_revision_files "
                "WHERE skill_id=:skill_id"
            ),
            {"skill_id": sid},
        )).scalar_one()
        draft_ciphertext = (await c.execute(
            text(
                "SELECT content_ciphertext FROM skill_draft_files "
                "WHERE skill_id=:skill_id"
            ),
            {"skill_id": sid},
        )).scalar_one()
        plaintext_columns = (await c.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name IN "
                "('skill_revision_files', 'skill_draft_files') "
                "AND column_name='content'"
            )
        )).scalar_one()
    assert published_body.decode() not in revision_ciphertext
    assert draft_body.decode() not in draft_ciphertext
    assert plaintext_columns == 0

    async with session_scope(tenant_id=str(tenant_id), user_id=str(user_id)) as s:
        repo = SkillsRepo(s)
        rows = await repo.list_for_user(user_id)
        assert any(r["name"] == "greet" for r in rows)
        assert await repo.read_bundle_file(sid, "SKILL.md") == published_body
        assert await repo.read_draft_files(sid) == [
            ("draft.txt", "text/plain", draft_body)
        ]
        await repo.soft_delete(sid)
        await s.commit()

    async with session_scope(tenant_id=str(tenant_id), user_id=str(user_id)) as s:
        repo = SkillsRepo(s)
        assert all(r["name"] != "greet" for r in await repo.list_for_user(user_id))
