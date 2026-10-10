"""Encrypted Agent-native Knowledge search tests."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.services.kb_search import KbSearchService, _rank, _tokens
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models_kb import KbChunk
from vibecanvas_api.storage.repo_kb import KbRepo


async def _seed_tenant_and_user(pg_engine, tenant_id, user_id) -> None:
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": tenant_id},
        )
        await connection.execute(text(
            "INSERT INTO users(user_id, tenant_id, email) VALUES (:u, :t, :e)"
        ), {"u": user_id, "t": tenant_id, "e": f"kb-{uuid.uuid4().hex[:8]}@example.com"})


def test_tokenizer_supports_identifiers_and_chinese():
    assert "vibecanvas_123" in _tokens("VIBECANVAS_123")
    assert _tokens("VIBECANVAS_123.") == ["vibecanvas_123"]
    assert "知识" in _tokens("知识库检索")


def test_rank_avoids_substring_false_positive_and_prefers_exact_phrase():
    assert _rank("match", "nomatch", "a.txt", {}) is None
    exact = _rank("release policy", "The release policy is here", "a.txt", {})
    partial = _rank("release policy", "Release notes", "a.txt", {})
    assert exact is not None and partial is not None
    assert exact[0] > partial[0]
    assert exact[1] == "exact_phrase"


@pytest.mark.asyncio
async def test_empty_kb_ids_rejected(pg_engine):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as session:
        with pytest.raises(ValueError):
            await KbSearchService(session).search_async(kb_ids=[], query="x")


@pytest.mark.asyncio
async def test_lexical_search_ranks_exact_and_filename_matches(pg_engine, monkeypatch):
    from unittest.mock import AsyncMock
    decrypt_file = KbRepo._materialize_file
    calls = AsyncMock()
    async def counted(repo, file):
        await calls(file.id)
        return await decrypt_file(repo, file)
    monkeypatch.setattr(KbRepo, "_materialize_file", counted)
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = KbRepo(session)
        kb = await repo.create_kb(tenant_id=tenant_id, user_id=user_id, name="X")
        source = await repo.create_file(
            kb_id=kb.id, tenant_id=tenant_id, user_id=user_id,
            name="release-policy.md", parser_type="markdown", mime_type="text/markdown",
            file_size=1, content_hash="0" * 64, status="indexed",
        )
        await repo.bulk_insert_chunks([
            KbChunk(file_id=source.id, kb_id=kb.id, tenant_id=tenant_id,
                    chunk_index=0, text="The release policy requires two approvals.",
                    chunk_metadata={"heading": "Deployment"}),
            KbChunk(file_id=source.id, kb_id=kb.id, tenant_id=tenant_id,
                    chunk_index=1, text="Unrelated release notes.", chunk_metadata={}),
        ])
        await session.commit()
        kb_id = kb.id

    async with session_scope(tenant_id=str(tenant_id)) as session:
        results = await KbSearchService(session).search_async(
            kb_ids=[str(kb_id)], query="release policy", top_k=10,
        )
    assert [result.text for result in results][:1] == [
        "The release policy requires two approvals."
    ]
    assert results[0].match_kind == "exact_phrase"
    assert results[0].score > 0
    assert calls.await_count == 1


@pytest.mark.asyncio
async def test_softdeleted_kb_returns_empty(pg_engine):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = KbRepo(session)
        kb = await repo.create_kb(tenant_id=tenant_id, user_id=user_id, name="X")
        source = await repo.create_file(
            kb_id=kb.id, tenant_id=tenant_id, user_id=user_id, name="a.txt",
            parser_type="txt", mime_type="text/plain", file_size=1,
            content_hash="1" * 64, status="indexed",
        )
        await repo.bulk_insert_chunks([KbChunk(
            file_id=source.id, kb_id=kb.id, tenant_id=tenant_id,
            chunk_index=0, text="needle", chunk_metadata={},
        )])
        await repo.soft_delete_kb(kb.id)
        await session.commit()
        kb_id = kb.id
    async with session_scope(tenant_id=str(tenant_id)) as session:
        assert await KbSearchService(session).search_async(
            kb_ids=[str(kb_id)], query="needle"
        ) == []
