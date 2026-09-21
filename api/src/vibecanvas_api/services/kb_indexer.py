"""KB indexer orchestrator — parse → chunk → encrypted write.

Async orchestrator called from the background worker task body via
``run_in_short_session(lambda s: KbIndexer(s, ...).index_file(file_id))``.
The lambda body IS async (it receives an ``AsyncSession``) because
``run_in_short_session`` does ``asyncio.run(coro)`` internally; the
caller (the background worker task) stays sync.

``MAX_CHUNKS_PER_FILE`` bounds parse/chunk memory and encrypted search cost.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.services.kb_chunker import RecursiveTokenChunker
from vibecanvas_api.services.kb_summary import summarize_knowledge
from vibecanvas_api.services.object_store import ObjectStore
from vibecanvas_api.services.parsers import (
    EmptyDocumentError,
    EncryptedDocumentError,
    PARSER_REGISTRY,
    ParseError,
    ParsedSegment,
)
from vibecanvas_api.storage.models_kb import KbChunk, KbFile, KnowledgeBase
from vibecanvas_api.storage.repo_kb import KbRepo


# Sentinel — refuses absurdly large docs to bound embedding cost.
MAX_CHUNKS_PER_FILE = 5000


class IndexingError(Exception):
    """Aggregate failure that the background worker task catches and writes as
    ``error_message`` on the KbFile. ``i18n_key`` maps to stable
    error codes so the frontend can localise the user-facing message.

    Default key is ``kb_error_unknown``; callers (or
    :func:`map_parse_exception`) pass a specific key for known failure
    modes (password-protected, image-only PDF, too many chunks, etc.)."""

    i18n_key: str = "kb_error_unknown"

    def __init__(self, message: str, i18n_key: str | None = None):
        super().__init__(message)
        if i18n_key:
            self.i18n_key = i18n_key


def map_parse_exception(exc: Exception) -> IndexingError:
    """Translate T2's parser exception hierarchy into an
    :class:`IndexingError` with the right ``i18n_key`` for the frontend
    user-facing error codes.

    Ordered most-specific first because ``EncryptedDocumentError`` and
    ``EmptyDocumentError`` are subclasses of :class:`ParseError`."""
    if isinstance(exc, EncryptedDocumentError):
        return IndexingError(str(exc), i18n_key="kb_error_password_protected")
    if isinstance(exc, EmptyDocumentError):
        return IndexingError(str(exc), i18n_key="kb_error_image_only_pdf")
    if isinstance(exc, ParseError):
        return IndexingError(str(exc), i18n_key="kb_error_parse_failed")
    return IndexingError(str(exc), i18n_key="kb_error_unknown")


class KbIndexer:
    """Async orchestrator. Called from the background worker task body via
    ``run_in_short_session``.

    Parsed chunks are encrypted directly.  Retrieval uses Agent-native
    lexical search and therefore has no model/provider dependency.
    """

    def __init__(
        self,
        session: AsyncSession,
        object_store: ObjectStore,
        chunker: RecursiveTokenChunker | None = None,
    ):
        self.session = session
        self.object_store = object_store
        self.chunker = chunker or RecursiveTokenChunker()
        self.repo = KbRepo(session)

    async def index_file(self, file_id: uuid.UUID) -> int:
        """Parse → chunk → encrypted write. Returns chunk_count on
        success; raises :class:`IndexingError` on any terminal failure.

        The background worker task body catches ``IndexingError``, cleans any
        partial chunks via ``delete_chunks_for_file``, and writes
        ``error_message`` to the ``kb_files`` row. Unknown exceptions
        propagate up — same cleanup path, different error_message."""
        kf, _kb = await self._get_active_file(file_id)

        # parse
        segments = await self._parse(kf)
        # chunk
        chunks = self.chunker.split(segments)
        if len(chunks) > MAX_CHUNKS_PER_FILE:
            raise IndexingError(
                f"document too large ({len(chunks)} chunks > "
                f"{MAX_CHUNKS_PER_FILE})",
                i18n_key="kb_error_too_many_chunks",
            )
        if not chunks:
            raise IndexingError(
                "empty document after chunking",
                i18n_key="kb_error_empty_document",
            )
        # Write chunks — chunk_index is pulled out of metadata to its own
        # column; the rest of metadata is preserved opaquely (T5 surfaces
        # it in search results).
        kb_chunks = []
        for c in chunks:
            row = KbChunk(
                file_id=kf.id,
                kb_id=kf.kb_id,
                tenant_id=kf.tenant_id,
                chunk_index=c.metadata["chunk_index"],
            )
            row.text = c.text
            row.chunk_metadata = {
                k: v for k, v in c.metadata.items() if k != "chunk_index"
            }
            kb_chunks.append(row)
        await self.repo.bulk_insert_chunks(kb_chunks)
        await self.repo.set_summary_if_empty(
            kf.kb_id,
            summarize_knowledge(chunk.text for chunk in chunks[:12]),
        )
        return len(kb_chunks)

    async def _get_active_file(
        self, file_id: uuid.UUID,
    ) -> tuple[KbFile, KnowledgeBase]:
        """Cross-check both the file AND its KB are not soft-deleted.
        If a user soft-deletes a KB or file between enqueue and
        worker pickup, this guard turns the run into a no-op instead of
        spending embedding budget on a tombstoned doc."""
        result = await self.session.execute(
            select(KbFile, KnowledgeBase)
            .join(KnowledgeBase, KnowledgeBase.id == KbFile.kb_id)
            .where(
                KbFile.id == file_id,
                KbFile.deleted_at.is_(None),
                KnowledgeBase.deleted_at.is_(None),
            )
        )
        row = result.first()
        if not row:
            raise IndexingError(
                "file deleted before indexing started",
                i18n_key="kb_error_unknown",
            )
        return row[0], row[1]

    async def _parse(self, kf: KbFile) -> list[ParsedSegment]:
        """Fetch blob from object store and dispatch to the parser
        registered for ``kf.parser_type``. Any parser exception is
        translated via :func:`map_parse_exception` to a localised
        :class:`IndexingError`."""
        if not kf.object_store_key:
            raise IndexingError("missing object store key")
        blob = self.object_store.fetch_bytes(kf.object_store_key)
        parser_cls = PARSER_REGISTRY.get(kf.parser_type)
        if parser_cls is None:
            raise IndexingError(
                f"unknown parser_type {kf.parser_type}",
                i18n_key="kb_error_unsupported_type",
            )
        try:
            return parser_cls().parse(blob)
        except Exception as e:
            raise map_parse_exception(e) from e
