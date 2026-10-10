"""Bounded ranking keeps the same ordering and yields to other requests."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from vibecanvas_api.services import kb_search


@pytest.mark.asyncio
async def test_batched_top_k_matches_complete_sort_and_yields(monkeypatch):
    rows = [(SimpleNamespace(id=str(uuid.UUID(int=i + 1)), file_id='f', kb_id='k',
                             text=f'知识库 release policy {i % 17}', chunk_metadata={}),
             SimpleNamespace(name=f'{i % 3}.md')) for i in range(1200)]
    monkeypatch.setattr(kb_search.KbRepo, 'search_chunks', AsyncMock(return_value=rows))
    expected = []
    for chunk, file in rows:
        score, _, _ = kb_search._rank('知识库 policy 16', chunk.text, file.name, {})
        expected.append((-score, file.name.casefold(), chunk.id))
    expected.sort()
    yields = 0
    async def other_request():
        nonlocal yields
        for _ in range(10):
            await asyncio.sleep(0)
            yields += 1
    task = asyncio.create_task(other_request())
    actual = await kb_search.KbSearchService(None).search_async([str(uuid.uuid4())], '知识库 policy 16', 5)
    assert yields > 0
    assert [r.chunk_id for r in actual] == [r[2] for r in expected[:5]]
    await task


@pytest.mark.asyncio
async def test_empty_and_nonmatching_queries_return_no_results(monkeypatch):
    rows = [(SimpleNamespace(id='c', file_id='f', kb_id='k', text='nomatch', chunk_metadata={}),
             SimpleNamespace(name='plain.txt'))]
    monkeypatch.setattr(kb_search.KbRepo, 'search_chunks', AsyncMock(return_value=rows))
    service = kb_search.KbSearchService(None)
    for query in ('', 'match'):
        assert await service.search_async([str(uuid.uuid4())], query, 5) == []
