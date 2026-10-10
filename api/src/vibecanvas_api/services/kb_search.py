"""Encrypted lexical search for Agent-native Knowledge discovery.

Knowledge source text stays encrypted at rest.  An authorized request loads a
bounded corpus, decrypts it in application memory, and ranks exact phrases,
query tokens, filenames, and headings.  No embedding model, vector database,
or third-party indexing request is involved.

The public ``search_async`` entry point is shared by HTTP and Platform MCP.
Keeping the existing service and route names preserves Workflow/API business
compatibility while changing the retrieval implementation from vector RAG to
an Agent-friendly grep/read loop.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from heapq import nsmallest
import re
import uuid

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.storage.repo_kb import KbRepo


MAX_ENCRYPTED_SEARCH_CHUNKS = 20_000

_LATIN_TOKEN = re.compile(
    r"[a-z0-9](?:[a-z0-9_.:/@+-]*[a-z0-9])?",
    re.IGNORECASE,
)
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_COMMON_TERMS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "what", "when", "where", "which", "who", "why", "with",
}


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _tokens(value: str) -> list[str]:
    """Tokenize identifiers and natural-language text without model calls.

    Latin identifiers keep path/punctuation characters useful to grep-style
    discovery.  CJK runs contribute characters and bigrams, which makes short
    Chinese queries useful without requiring a language-specific segmenter.
    """
    normalized = _normalize(value)
    tokens = [
        token for token in _LATIN_TOKEN.findall(normalized)
        if token not in _COMMON_TERMS
    ]
    for run in _CJK_RUN.findall(normalized):
        tokens.extend(run)
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


class EncryptedKbSearchLimitError(RuntimeError):
    pass


class KbSearchResult(BaseModel):
    chunk_id: str
    file_id: str
    file_name: str
    kb_id: str
    text: str
    score: float
    match_kind: str
    matched_terms: list[str]
    chunk_metadata: dict


def _rank(
    query: str, text: str, file_name: str, metadata: dict,
    *, prepared: tuple[str, list[str]] | None = None,
) -> tuple[float, str, list[str]] | None:
    normalized_query, query_terms = prepared or (_normalize(query), list(dict.fromkeys(_tokens(query))))
    normalized_text = _normalize(text)
    normalized_file = _normalize(file_name)
    metadata_text = _normalize(" ".join(str(value) for value in metadata.values()))
    if not normalized_query:
        return None

    searchable = f"{normalized_file}\n{metadata_text}\n{normalized_text}"
    counts = Counter(_tokens(searchable))
    matched = [term for term in query_terms if counts[term] > 0]
    # A textual substring only counts as an exact phrase when its lexical
    # tokens also match boundaries. This keeps grep for ``match`` from
    # accepting ``nomatch`` while still supporting punctuation-only literals.
    token_boundary_ok = not query_terms or all(counts[term] > 0 for term in query_terms)
    exact_text = token_boundary_ok and normalized_query in normalized_text
    exact_file = token_boundary_ok and normalized_query in normalized_file
    exact_metadata = token_boundary_ok and normalized_query in metadata_text

    # A punctuation-only or quoted query can still behave like fixed-string
    # grep even when it yields no language tokens.
    if not matched and not (exact_text or exact_file or exact_metadata):
        return None

    coverage = len(matched) / len(query_terms) if query_terms else 0.0
    frequency = min(sum(counts[term] for term in matched), 8) / 8
    score = (
        0.48 * float(exact_text)
        + 0.12 * float(exact_file)
        + 0.08 * float(exact_metadata)
        + 0.27 * coverage
        + 0.05 * frequency
    )
    if exact_text or exact_file or exact_metadata:
        match_kind = "exact_phrase"
    elif coverage == 1.0:
        match_kind = "all_terms"
    else:
        match_kind = "partial_terms"
    return min(score, 1.0), match_kind, matched


class KbSearchService:
    """Tenant-scoped encrypted lexical search; caller owns the session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_async(
        self,
        kb_ids: list[str],
        query: str,
        top_k: int = 5,
    ) -> list[KbSearchResult]:
        if not kb_ids:
            raise ValueError("kb_ids must contain at least one id")

        parsed_ids = [uuid.UUID(value) for value in kb_ids]
        rows = await KbRepo(self.session).search_chunks(
            kb_ids=parsed_ids,
            limit=MAX_ENCRYPTED_SEARCH_CHUNKS + 1,
        )
        if len(rows) > MAX_ENCRYPTED_SEARCH_CHUNKS:
            raise EncryptedKbSearchLimitError(
                "encrypted_knowledge_search_corpus_too_large"
            )

        prepared = (_normalize(query), list(dict.fromkeys(_tokens(query))))
        ranked: list[KbSearchResult] = []
        def order(item: KbSearchResult):
            return (-item.score, item.file_name.casefold(), item.chunk_id)

        for index, (chunk, file) in enumerate(rows):
            # Bound temporary result objects and let other requests run between
            # CPU batches. No new pool, index, or plaintext persistent cache.
            if index % 128 == 0:
                ranked = nsmallest(top_k, ranked, key=order)
                await asyncio.sleep(0)
            metadata = chunk.chunk_metadata or {}
            match = _rank(query, chunk.text, file.name, metadata, prepared=prepared)
            if match is None:
                continue
            score, match_kind, matched_terms = match
            ranked.append(KbSearchResult(
                chunk_id=str(chunk.id),
                file_id=str(chunk.file_id),
                file_name=file.name,
                kb_id=str(chunk.kb_id),
                text=chunk.text,
                score=score,
                match_kind=match_kind,
                matched_terms=matched_terms,
                chunk_metadata=metadata,
            ))
        return nsmallest(top_k, ranked, key=order)
