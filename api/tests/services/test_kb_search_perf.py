"""Opt-in encrypted lexical search baseline, including SQL/decryption/ranking.

Set KB_PERF_BASELINE=1. The 3-second ceiling is a coarse regression guard,
not an HNSW/vector benchmark or a production latency promise.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.services.kb_search import KbSearchService
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models_kb import KbChunk
from vibecanvas_api.storage.repo_kb import KbRepo


PERF_OPT_IN = os.getenv("KB_PERF_BASELINE")


async def _seed_tenant_and_user(pg_engine, tenant_id, user_id) -> None:
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
             "e": f"kb-perf-{uuid.uuid4().hex[:6]}@example.com"},
        )


@pytest.mark.skipif(
    not PERF_OPT_IN,
    reason="set KB_PERF_BASELINE=1 to run the perf baseline",
)
@pytest.mark.asyncio
async def test_encrypted_lexical_search_at_10k(pg_engine):
    """10k encrypted chunks, top-5 search with a coarse latency ceiling."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = KbRepo(s)
        kb = await repo.create_kb(
            tenant_id=tenant_id, user_id=user_id, name="Perf",
        )
        f = await repo.create_file(
            kb_id=kb.id, tenant_id=tenant_id, user_id=user_id,
            name="x", parser_type="txt", mime_type="text/plain",
            file_size=1, content_hash="p" * 64, status="indexed",
        )
        chunks = [
            KbChunk(
                file_id=f.id, kb_id=kb.id, tenant_id=tenant_id,
                chunk_index=i, text=f"release policy document number {i}",
                chunk_metadata={},
            )
            for i in range(10_000)
        ]
        await repo.bulk_insert_chunks(chunks)
        await s.commit()
        kb_id = kb.id

    async with session_scope(tenant_id=str(tenant_id)) as s:
        svc = KbSearchService(s)
        start = time.perf_counter()
        await svc.search_async(
            kb_ids=[str(kb_id)], query="release policy 9999", top_k=5,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0

    print(f"\nEncrypted lexical search 10k chunks: {elapsed_ms:.1f} ms")
    assert elapsed_ms < 3000, (
        f"perf regression: encrypted lexical 10k took {elapsed_ms:.1f} ms (> 3000 ms)"
    )
