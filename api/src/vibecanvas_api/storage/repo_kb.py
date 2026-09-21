"""KB / RAG repository — async CRUD with soft-delete.

Caller owns the transaction: every public method ``flush()``-es but does
NOT ``commit()``. The DI request session commits at request end; the
background worker indexer commits explicitly via ``session_scope`` /
``run_in_short_session``.

Tenant scoping is delegated to Postgres FORCE RLS — the session must be
bound to a tenant via ``app.tenant_id`` GUC (set by
``storage.db.session_scope(tenant_id=...)`` or the per-request
dependency). Every method here just queries; RLS hides rows from the
wrong tenant.

Soft-delete contract (spec sec 4.6): "delete" is an UPDATE of
``deleted_at``. Cascading rows physically vanish only when the GC sweeper
(T11) issues real DELETEs after 30 days, at which point ``ON DELETE
CASCADE`` finally fires. ``soft_delete_kb`` therefore must propagate the
``deleted_at`` UPDATE to ``kb_files`` itself so the public read path
(``list_files`` filters on ``deleted_at IS NULL``) hides files of a
soft-deleted KB immediately.

Returns: ORM instances (mirrors the spec's signatures and is the simpler
shape for the indexer / search services). If a future task needs the
``mappings()`` dict shape that ``McpServersRepo`` returns, add a parallel
method rather than changing these signatures.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import (
    content_encryption_service,
    content_lookup_digest,
)
from vibecanvas_api.storage.models_kb import KbChunk, KbFile, KnowledgeBase


class KbRepo:
    """Tenant-scoped (via RLS); all reads/writes go through the bound
    session.

    Caller owns the transaction: every public method ``flush()``-es but
    does NOT ``commit()``. See module docstring for the soft-delete
    contract.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _materialize_kb(self, kb: KnowledgeBase) -> KnowledgeBase:
        value = await content_encryption_service().decrypt_json(
            self.session,
            key_id=kb.private_key_id,
            tenant_id=kb.tenant_id,
            resource_type="knowledge_base",
            resource_id=str(kb.id),
            purpose="knowledge_base_private",
            record_id=str(kb.id),
            ciphertext=kb.private_ciphertext,
            nonce=kb.private_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("Knowledge Base ciphertext must contain an object")
        kb.name = str(value.get("name") or "")
        kb.description = value.get("description")
        kb.summary = value.get("summary")
        return kb

    async def _store_kb_private(
        self,
        kb: KnowledgeBase,
        *,
        name: str,
        description: str | None,
        summary: str | None,
    ) -> None:
        encrypted = await content_encryption_service().encrypt_json(
            self.session,
            tenant_id=kb.tenant_id,
            resource_type="knowledge_base",
            resource_id=str(kb.id),
            purpose="knowledge_base_private",
            record_id=str(kb.id),
            value={
                "name": name,
                "description": description,
                "summary": summary,
            },
        )
        kb.name_lookup_hash = content_lookup_digest(
            tenant_id=kb.tenant_id,
            namespace="knowledge_base_name",
            value=name,
        )
        kb.private_ciphertext = encrypted.ciphertext
        kb.private_nonce = encrypted.nonce
        kb.private_key_id = encrypted.key_id
        kb.name = name
        kb.description = description
        kb.summary = summary

    async def _materialize_file(self, file: KbFile) -> KbFile:
        value = await content_encryption_service().decrypt_json(
            self.session,
            key_id=file.private_key_id,
            tenant_id=file.tenant_id,
            resource_type="knowledge_base",
            resource_id=str(file.kb_id),
            purpose="knowledge_base_file_private",
            record_id=str(file.id),
            ciphertext=file.private_ciphertext,
            nonce=file.private_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("Knowledge Base file ciphertext must contain an object")
        file.name = str(value.get("name") or "")
        file.error_message = value.get("error_message")
        return file

    async def _store_file_private(
        self,
        file: KbFile,
        *,
        name: str,
        error_message: str | None,
    ) -> None:
        encrypted = await content_encryption_service().encrypt_json(
            self.session,
            tenant_id=file.tenant_id,
            resource_type="knowledge_base",
            resource_id=str(file.kb_id),
            purpose="knowledge_base_file_private",
            record_id=str(file.id),
            value={"name": name, "error_message": error_message},
        )
        file.private_ciphertext = encrypted.ciphertext
        file.private_nonce = encrypted.nonce
        file.private_key_id = encrypted.key_id
        file.name = name
        file.error_message = error_message

    async def materialize_chunk(self, chunk: KbChunk) -> KbChunk:
        value = await content_encryption_service().decrypt_json(
            self.session,
            key_id=chunk.content_key_id,
            tenant_id=chunk.tenant_id,
            resource_type="knowledge_base",
            resource_id=str(chunk.kb_id),
            purpose="knowledge_base_chunk",
            record_id=str(chunk.id),
            ciphertext=chunk.content_ciphertext,
            nonce=chunk.content_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("Knowledge Base chunk ciphertext must contain an object")
        chunk.text = str(value.get("text") or "")
        chunk.chunk_metadata = value.get("chunk_metadata") or {}
        return chunk

    # ---------------- knowledge_bases ----------------

    async def create_kb(
        self, *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        description: str | None = None,
    ) -> KnowledgeBase:
        kb = KnowledgeBase(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
        )
        await self._store_kb_private(
            kb,
            name=name,
            description=description,
            summary=None,
        )
        self.session.add(kb)
        await self.session.flush()
        return kb

    async def get_active(self, kb_id: uuid.UUID) -> Optional[KnowledgeBase]:
        result = await self.session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.deleted_at.is_(None),
            )
        )
        kb = result.scalar_one_or_none()
        return await self._materialize_kb(kb) if kb is not None else None

    async def list_active(
        self,
        authorized_kb_ids: Sequence[str] | None = None,
    ) -> Sequence[KnowledgeBase]:
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.deleted_at.is_(None)
        )
        if authorized_kb_ids is not None:
            parsed_ids: list[uuid.UUID] = []
            for value in authorized_kb_ids:
                try:
                    parsed_ids.append(uuid.UUID(str(value)))
                except ValueError:
                    continue
            if not parsed_ids:
                return []
            stmt = stmt.where(KnowledgeBase.id.in_(parsed_ids))
        result = await self.session.execute(
            stmt.order_by(KnowledgeBase.created_at.desc())
        )
        rows = list(result.scalars().all())
        for row in rows:
            await self._materialize_kb(row)
        return rows

    async def list_file_stats(
        self,
        kb_ids: Sequence[uuid.UUID],
    ) -> dict[str, dict[str, int | datetime | None]]:
        """Return list-page health aggregates without decrypting every file.

        File names and errors remain encrypted private content. Status/count
        columns are operational metadata, so one grouped query can power the
        Knowledge catalog without an N+1 detail request waterfall.
        """
        if not kb_ids:
            return {}
        rows = (
            await self.session.execute(
                select(
                    KbFile.kb_id,
                    func.count(KbFile.id).label("file_count"),
                    func.coalesce(func.sum(KbFile.chunk_count), 0).label("chunk_count"),
                    func.sum(case((KbFile.status == "stored", 1), else_=0)).label("stored_count"),
                    func.sum(case((KbFile.status == "pending", 1), else_=0)).label("pending_count"),
                    func.sum(case((KbFile.status == "indexing", 1), else_=0)).label("indexing_count"),
                    func.sum(case((KbFile.status == "indexed", 1), else_=0)).label("indexed_count"),
                    func.sum(case((KbFile.status == "failed", 1), else_=0)).label("failed_count"),
                    func.max(KbFile.updated_at).label("latest_updated_at"),
                )
                .where(
                    KbFile.kb_id.in_(tuple(kb_ids)),
                    KbFile.deleted_at.is_(None),
                )
                .group_by(KbFile.kb_id)
            )
        ).mappings().all()
        return {
            str(row["kb_id"]): {
                "file_count": int(row["file_count"] or 0),
                "chunk_count": int(row["chunk_count"] or 0),
                "stored_count": int(row["stored_count"] or 0),
                "pending_count": int(row["pending_count"] or 0),
                "indexing_count": int(row["indexing_count"] or 0),
                "indexed_count": int(row["indexed_count"] or 0),
                "failed_count": int(row["failed_count"] or 0),
                "latest_updated_at": row["latest_updated_at"],
            }
            for row in rows
        }

    async def soft_delete_kb(self, kb_id: uuid.UUID) -> None:
        """Soft-delete the KB AND cascade the ``deleted_at`` UPDATE to
        every live ``kb_files`` row under it (spec sec 4.6 contract).

        The physical CASCADE only fires when the GC sweeper later issues
        a real DELETE, so without this cascade UPDATE the files would
        still appear in ``list_files`` after the KB is "deleted"."""
        now = datetime.now(timezone.utc)
        await self.session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id)
            .values(deleted_at=now)
        )
        await self.session.execute(
            update(KbFile)
            .where(KbFile.kb_id == kb_id, KbFile.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await self.session.flush()

    async def update_kb(
        self, kb_id: uuid.UUID, *,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        if name is None and description is None:
            return
        kb = await self.get_active(kb_id)
        if kb is None:
            return
        await self._store_kb_private(
            kb,
            name=name if name is not None else kb.name,
            description=(
                description if description is not None else kb.description
            ),
            summary=kb.summary,
        )
        await self.session.flush()

    async def bump_package_version(self, kb_id: uuid.UUID) -> int:
        """Serialize and increment the authoritative file-tree revision."""
        kb = (
            await self.session.execute(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.id == kb_id,
                    KnowledgeBase.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if kb is None:
            raise LookupError("knowledge_not_found")
        kb.package_version += 1
        await self.session.flush()
        return kb.package_version

    async def set_summary_if_empty(
        self,
        kb_id: uuid.UUID,
        summary: str,
    ) -> bool:
        """Persist the first successful source summary without overwriting edits."""
        if not summary:
            return False
        kb = await self.get_active(kb_id)
        if kb is None or kb.summary:
            return False
        await self._store_kb_private(
            kb,
            name=kb.name,
            description=kb.description,
            summary=summary[:500],
        )
        await self.session.flush()
        return True

    # ---------------- kb_files ----------------

    async def create_file(
        self, *,
        kb_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        parser_type: str,
        mime_type: str,
        file_size: int,
        content_hash: str,
        status: str = "pending",
        object_store_key: str | None = None,
    ) -> KbFile:
        kf = KbFile(
            id=uuid.uuid4(),
            kb_id=kb_id, tenant_id=tenant_id, user_id=user_id,
            parser_type=parser_type, mime_type=mime_type,
            file_size=file_size, content_hash=content_hash,
            status=status, object_store_key=object_store_key,
        )
        await self._store_file_private(
            kf,
            name=name,
            error_message=None,
        )
        self.session.add(kf)
        await self.session.flush()
        return kf

    async def get_file(self, file_id: uuid.UUID) -> Optional[KbFile]:
        """Look up a live (non-soft-deleted) file by id. Returns None if
        missing — caller surfaces 404 ``kb_file_not_found``. RLS filters
        cross-tenant; cross-KB check is caller's responsibility."""
        result = await self.session.execute(
            select(KbFile).where(
                KbFile.id == file_id,
                KbFile.deleted_at.is_(None),
            )
        )
        file = result.scalar_one_or_none()
        return await self._materialize_file(file) if file is not None else None

    async def list_files(
        self, kb_id: uuid.UUID, status: str | None = None,
    ) -> Sequence[KbFile]:
        stmt = select(KbFile).where(
            KbFile.kb_id == kb_id, KbFile.deleted_at.is_(None),
        )
        if status:
            stmt = stmt.where(KbFile.status == status)
        stmt = stmt.order_by(KbFile.created_at.desc())
        result = await self.session.execute(stmt)
        rows = list(result.scalars().all())
        for row in rows:
            await self._materialize_file(row)
        return rows

    async def set_object_store_key(
        self, file_id: uuid.UUID, object_key: str,
    ) -> None:
        await self.session.execute(
            update(KbFile).where(KbFile.id == file_id)
            .values(object_store_key=object_key)
        )
        await self.session.flush()

    async def set_file_status(
        self, file_id: uuid.UUID, status: str,
        error_message: str | None = None,
        chunk_count: int | None = None,
    ) -> None:
        file = await self.session.get(KbFile, file_id)
        if file is None:
            raise LookupError(f"Knowledge Base file {file_id} not found")
        values: dict = {"status": status}
        if error_message is not None:
            await self._materialize_file(file)
            await self._store_file_private(
                file,
                name=file.name,
                error_message=error_message[:1024],
            )
        if chunk_count is not None:
            values["chunk_count"] = chunk_count
        if status == "indexed":
            values["indexed_at"] = datetime.now(timezone.utc)
        for key, value in values.items():
            setattr(file, key, value)
        await self.session.flush()

    async def soft_delete_file(self, file_id: uuid.UUID) -> None:
        await self.session.execute(
            update(KbFile).where(KbFile.id == file_id)
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await self.session.flush()

    async def fail_pending_if_stale(
        self,
        file_id: uuid.UUID,
        *,
        older_than: datetime,
        error_message: str,
    ) -> bool:
        """Atomically fail a still-pending orphan without a retry race."""
        result = await self.session.execute(
            select(KbFile)
            .where(
                KbFile.id == file_id,
                KbFile.status == "pending",
                KbFile.created_at < older_than,
                KbFile.deleted_at.is_(None),
            )
            .with_for_update()
        )
        file = result.scalar_one_or_none()
        if file is None:
            return False
        await self._materialize_file(file)
        await self._store_file_private(
            file,
            name=file.name,
            error_message=error_message[:1024],
        )
        file.status = "failed"
        await self.session.flush()
        return True

    # ---------------- kb_chunks ----------------

    async def bulk_insert_chunks(self, chunks: list[KbChunk]) -> None:
        for chunk in chunks:
            chunk.id = chunk.id or uuid.uuid4()
            encrypted = await content_encryption_service().encrypt_json(
                self.session,
                tenant_id=chunk.tenant_id,
                resource_type="knowledge_base",
                resource_id=str(chunk.kb_id),
                purpose="knowledge_base_chunk",
                record_id=str(chunk.id),
                value={
                    "text": chunk.text,
                    "chunk_metadata": chunk.chunk_metadata or {},
                },
            )
            chunk.content_ciphertext = encrypted.ciphertext
            chunk.content_nonce = encrypted.nonce
            chunk.content_key_id = encrypted.key_id
        self.session.add_all(chunks)
        await self.session.flush()

    async def search_chunks(
        self,
        *,
        kb_ids: Sequence[uuid.UUID],
        limit: int,
    ) -> list[tuple[KbChunk, KbFile]]:
        """Load an explicitly bounded encrypted search corpus."""
        rows = (
            await self.session.execute(
                select(KbChunk, KbFile)
                .join(KbFile, KbFile.id == KbChunk.file_id)
                .join(KnowledgeBase, KnowledgeBase.id == KbChunk.kb_id)
                .where(
                    KbChunk.kb_id.in_(tuple(kb_ids)),
                    KbFile.deleted_at.is_(None),
                    KnowledgeBase.deleted_at.is_(None),
                )
                .order_by(KbChunk.id)
                .limit(limit)
            )
        ).all()
        result: list[tuple[KbChunk, KbFile]] = []
        for chunk, file in rows:
            await self.materialize_chunk(chunk)
            await self._materialize_file(file)
            result.append((chunk, file))
        return result

    async def delete_chunks_for_file(self, file_id: uuid.UUID) -> int:
        result = await self.session.execute(
            delete(KbChunk).where(KbChunk.file_id == file_id)
        )
        await self.session.flush()
        return result.rowcount or 0

    async def count_chunks(self, kb_id: uuid.UUID) -> int:
        """Count chunks that still belong to a live source file.

        Source deletion is intentionally soft so the GC sweeper can remove
        encrypted rows later.  Aggregate counters must follow the same
        visibility rule as retrieval, otherwise a deleted source disappears
        from the file list while its chunks remain visible in KB totals.
        """
        result = await self.session.execute(
            select(func.count())
            .select_from(KbChunk)
            .join(KbFile, KbFile.id == KbChunk.file_id)
            .where(
                KbChunk.kb_id == kb_id,
                KbFile.deleted_at.is_(None),
            )
        )
        return result.scalar_one()
