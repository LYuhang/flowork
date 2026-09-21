"""KB / RAG T4 — ``KbIndexer`` orchestrator: happy path + cost guard + soft-delete.

Coverage (3 cases — match the spec in plan Step 4.4):

* ``test_indexer_happy_path`` — create KB + file → run indexer with a
  mock object_store → assert returned ``chunk_count == 1``
  and ``count_chunks(kb.id) == 1``.
* ``test_indexer_too_large_doc_rejected`` — feed a blob that produces
  more than ``MAX_CHUNKS_PER_FILE`` chunks → assert
  ``IndexingError(i18n_key="kb_error_too_many_chunks")``.
* ``test_indexer_skips_softdeleted_kb`` — soft-delete the KB mid-flight,
  then run indexer → ``IndexingError`` (the JOIN against
  ``knowledge_bases.deleted_at IS NULL`` hides the row).

Fixture pattern mirrors ``tests/storage/test_repo_kb.py`` (T1): the
plan-spec ``tenant_session`` / ``tenant_id`` / ``user_id`` fixtures are
NOT present in this repo's ``conftest.py``, so we inline tenant + user
seeding through the RLS-bypassing ``pg_engine`` and drive ``KbRepo`` +
``KbIndexer`` through ``session_scope(tenant_id=...)``.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from vibecanvas_api.services.kb_indexer import (
    IndexingError,
    KbIndexer,
    MAX_CHUNKS_PER_FILE,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_kb import KbRepo


# --------------------------------------------------------------------- seed


async def _seed_tenant_and_user(pg_engine, tenant_id, user_id) -> None:
    """Insert tenant + user via the RLS-bypassing superuser engine (T1 pattern)."""
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
             "e": f"kb-indexer-{uuid.uuid4().hex[:6]}@example.com"},
        )


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_indexer_happy_path(pg_engine):
    """End-to-end: create KB → create file → run indexer (mocked store +
    parser) → assert one encrypted chunk persisted."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = KbRepo(s)
        kb = await repo.create_kb(
            tenant_id=tenant_id, user_id=user_id, name="X",
        )
        f = await repo.create_file(
            kb_id=kb.id, tenant_id=tenant_id, user_id=user_id,
            name="a.txt", parser_type="txt", mime_type="text/plain",
            file_size=20, content_hash="h" * 64, status="pending",
            object_store_key="kb/x/y/z/a.txt",
        )
        await s.commit()
        kb_id = kb.id
        file_id = f.id

    fake_store = MagicMock()
    fake_store.fetch_bytes.return_value = b"hello world this is content"
    async with session_scope(tenant_id=str(tenant_id)) as s:
        indexer = KbIndexer(s, fake_store)
        n = await indexer.index_file(file_id)
        await s.commit()
    assert n == 1

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = KbRepo(s)
        assert await repo.count_chunks(kb_id) == 1


@pytest.mark.asyncio
async def test_indexer_too_large_doc_rejected(pg_engine):
    """A blob that produces > MAX_CHUNKS_PER_FILE chunks must be rejected
    with ``IndexingError(i18n_key="kb_error_too_many_chunks")``."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = KbRepo(s)
        kb = await repo.create_kb(
            tenant_id=tenant_id, user_id=user_id, name="X",
        )
        f = await repo.create_file(
            kb_id=kb.id, tenant_id=tenant_id, user_id=user_id,
            name="huge.txt", parser_type="txt", mime_type="text/plain",
            file_size=10_000_000, content_hash="z" * 64, status="pending",
            object_store_key="key",
        )
        await s.commit()
        file_id = f.id

    fake_store = MagicMock()
    # Build a blob that will produce > MAX_CHUNKS_PER_FILE chunks: each
    # paragraph (separated by \n\n) is a chunk-sized run of characters.
    big_text = ("X" * 2000 + "\n\n") * (MAX_CHUNKS_PER_FILE + 10)
    fake_store.fetch_bytes.return_value = big_text.encode()
    async with session_scope(tenant_id=str(tenant_id)) as s:
        indexer = KbIndexer(s, fake_store)
        with pytest.raises(IndexingError) as excinfo:
            await indexer.index_file(file_id)
        assert excinfo.value.i18n_key == "kb_error_too_many_chunks"


@pytest.mark.asyncio
async def test_indexer_skips_softdeleted_kb(pg_engine):
    """If the KB has been soft-deleted before indexing starts, the
    JOIN-against-``knowledge_bases.deleted_at IS NULL`` cross-check in
    ``_get_active_file`` must raise ``IndexingError``."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = KbRepo(s)
        kb = await repo.create_kb(
            tenant_id=tenant_id, user_id=user_id, name="X",
        )
        f = await repo.create_file(
            kb_id=kb.id, tenant_id=tenant_id, user_id=user_id,
            name="a.txt", parser_type="txt", mime_type="text/plain",
            file_size=20, content_hash="h" * 64, status="pending",
            object_store_key="key",
        )
        await repo.soft_delete_kb(kb.id)
        await s.commit()
        file_id = f.id

    async with session_scope(tenant_id=str(tenant_id)) as s:
        indexer = KbIndexer(s, MagicMock())
        with pytest.raises(IndexingError):
            await indexer.index_file(file_id)
