"""Database-authoritative Skill versions with a latest-only VFS projection."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from collections.abc import Sequence
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import content_encryption_service
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.vfs_store import VfsRepo


SkillFiles = list[tuple[str, Optional[str], bytes]]


def skill_scope_id(user_id: str | uuid.UUID) -> str:
    """Private internal VFS scope containing only each Skill's latest version."""
    return f"__skills_{str(user_id).replace('-', '')}"


def _content_hash(files: SkillFiles, *, version: int | None = None) -> str:
    digest = hashlib.sha256()
    if version is not None:
        digest.update(f"version:{version}".encode())
        digest.update(b"\0")
    for path, content_type, data in sorted(files, key=lambda item: item[0]):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update((content_type or "").encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _manifest(files: SkillFiles) -> tuple[list[dict], int, SkillFiles]:
    manifest: list[dict] = []
    normalized: SkillFiles = []
    total = 0
    for path, content_type, data in files:
        media_type = (
            content_type
            or mimetypes.guess_type(path)[0]
            or "application/octet-stream"
        )
        content_hash = hashlib.sha256(data).hexdigest()
        manifest.append({
            "path": path,
            "content_type": media_type,
            "content_hash": content_hash,
            "size_bytes": len(data),
        })
        normalized.append((path, media_type, data))
        total += len(data)
    return manifest, total, normalized


class SkillsRepo:
    """Own Skill identity, immutable versions, drafts, and Runtime projection.

    Published and draft bytes live in PostgreSQL. The user VFS contains only a
    replaceable projection of each Skill's current revision so runtimes can use
    ordinary filesystem semantics without learning the version-storage model.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.vfs = VfsRepo(session, object_store=get_object_store())

    async def _encrypt_file(
        self,
        *,
        tenant_id: uuid.UUID,
        skill_id: uuid.UUID,
        purpose: str,
        record_id: str,
        data: bytes,
    ) -> dict:
        encrypted = await content_encryption_service().encrypt_bytes(
            self.session,
            tenant_id=tenant_id,
            resource_type="skill",
            resource_id=str(skill_id),
            purpose=purpose,
            record_id=record_id,
            plaintext=data,
        )
        return {
            "content_ciphertext": encrypted.ciphertext,
            "content_nonce": encrypted.nonce,
            "content_key_id": encrypted.key_id,
        }

    async def _decrypt_file(self, row, *, purpose: str, record_id: str) -> bytes:
        return await content_encryption_service().decrypt_bytes(
            self.session,
            key_id=row["content_key_id"],
            tenant_id=row["tenant_id"],
            resource_type="skill",
            resource_id=str(row["skill_id"]),
            purpose=purpose,
            record_id=record_id,
            ciphertext=str(row["content_ciphertext"]),
            nonce=str(row["content_nonce"]),
        )

    async def insert(
        self, *, tenant_id, user_id, name: str, description: str = "",
        version: int = 1, allowed_tools=None, source: str | None = None,
        source_id: str | None = None, source_url: str | None = None,
        source_revision: str | None = None,
        files: Optional[SkillFiles] = None,
    ) -> uuid.UUID:
        skill_id = uuid.uuid4()
        await self.session.execute(
            text(
                "INSERT INTO skills "
                "(skill_id, tenant_id, user_id, name, description, version, "
                "allowed_tools, source, source_id, source_url, source_revision) "
                "VALUES (:skill_id, :tenant_id, :user_id, :name, :description, "
                ":version, CAST(:allowed_tools AS jsonb), :source, :source_id, "
                ":source_url, :source_revision)"
            ),
            {
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "name": name,
                "description": description,
                "version": version,
                "allowed_tools": json.dumps(allowed_tools or []),
                "source": source,
                "source_id": source_id,
                "source_url": source_url,
                "source_revision": source_revision,
            },
        )
        await self._create_revision(
            skill_id=skill_id,
            tenant_id=tenant_id,
            user_id=user_id,
            version=version,
            files=files or [],
        )
        from vibecanvas_api.storage.repo_skill_installations import SkillInstallationsRepo
        await SkillInstallationsRepo(self.session).install(user_id, skill_id)
        return skill_id

    async def save_draft(
        self,
        *,
        skill_id: uuid.UUID,
        tenant_id: uuid.UUID,
        files: SkillFiles,
    ) -> dict | None:
        current = await self._lock_custom_skill(skill_id)
        if current is None:
            return None
        owner_user_id = current["user_id"]
        draft_hash = _content_hash(files)
        manifest, total, normalized = _manifest(files)
        await self.session.execute(
            text(
                "INSERT INTO skill_drafts "
                "(skill_id, tenant_id, user_id, base_revision_id, draft_hash, "
                "file_manifest, size_bytes) "
                "VALUES (:skill_id, :tenant_id, :user_id, :base_revision_id, "
                ":draft_hash, CAST(:manifest AS jsonb), :size_bytes) "
                "ON CONFLICT (skill_id) DO UPDATE SET "
                "base_revision_id=EXCLUDED.base_revision_id, "
                "draft_hash=EXCLUDED.draft_hash, "
                "file_manifest=EXCLUDED.file_manifest, "
                "size_bytes=EXCLUDED.size_bytes, updated_at=now()"
            ),
            {
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "user_id": owner_user_id,
                "base_revision_id": current["current_revision_id"],
                "draft_hash": draft_hash,
                "manifest": json.dumps(manifest),
                "size_bytes": total,
            },
        )
        await self.session.execute(
            text("DELETE FROM skill_draft_files WHERE skill_id=:skill_id"),
            {"skill_id": skill_id},
        )
        for path, media_type, data in normalized:
            envelope = await self._encrypt_file(
                tenant_id=tenant_id,
                skill_id=skill_id,
                purpose="skill_draft_file",
                record_id=f"{skill_id}:{path}",
                data=data,
            )
            await self.session.execute(
                text(
                    "INSERT INTO skill_draft_files "
                    "(skill_id, path, tenant_id, user_id, content_type, "
                    "content_hash, size_bytes, content_ciphertext, "
                    "content_nonce, content_key_id) "
                    "VALUES (:skill_id, :path, :tenant_id, :user_id, "
                    ":content_type, :content_hash, :size_bytes, "
                    ":content_ciphertext, :content_nonce, :content_key_id)"
                ),
                {
                    "skill_id": skill_id,
                    "path": path,
                    "tenant_id": tenant_id,
                    "user_id": owner_user_id,
                    "content_type": media_type,
                    "content_hash": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                    **envelope,
                },
            )
        return await self.get_draft(skill_id)

    async def publish_draft(
        self,
        *,
        skill_id: uuid.UUID,
        tenant_id: uuid.UUID,
        version: int,
        name: str,
        description: str,
        allowed_tools: list[str],
        expected_draft_hash: str,
        files: SkillFiles,
    ) -> dict | None:
        current = await self._lock_custom_skill(skill_id)
        if current is None:
            return None
        owner_user_id = current["user_id"]
        draft = await self.get_draft(skill_id, for_update=True)
        if draft is None:
            raise ValueError("No saved draft is available to publish")
        if draft["draft_hash"] != expected_draft_hash:
            raise RuntimeError(
                "The draft changed while it was being published; reload it and try again"
            )
        if draft["base_revision_id"] != current["current_revision_id"]:
            raise RuntimeError(
                "The published Skill changed after this draft was created; "
                "reload the latest version before publishing"
            )
        if version <= int(current["version"]):
            raise ValueError(
                f"Version must be greater than the current version "
                f"({current['version']})"
            )
        await self._create_revision(
            skill_id=skill_id,
            tenant_id=tenant_id,
            user_id=owner_user_id,
            version=version,
            files=files,
        )
        await self.session.execute(
            text(
                "UPDATE skills SET name=:name, description=:description, "
                "version=:version, allowed_tools=CAST(:allowed_tools AS jsonb), "
                "updated_at=now() WHERE skill_id=:skill_id AND user_id=:user_id "
                "AND deleted_at IS NULL"
            ),
            {
                "skill_id": skill_id,
                "user_id": owner_user_id,
                "name": name,
                "description": description,
                "version": version,
                "allowed_tools": json.dumps(allowed_tools),
            },
        )
        await self.session.execute(
            text("DELETE FROM skill_drafts WHERE skill_id=:skill_id"),
            {"skill_id": skill_id},
        )
        return await self.get(skill_id)

    async def _lock_custom_skill(
        self, skill_id: uuid.UUID,
    ) -> dict | None:
        row = (await self.session.execute(
            text(
                "SELECT * FROM skills WHERE skill_id=:skill_id "
                "AND source='custom' "
                "AND deleted_at IS NULL FOR UPDATE"
            ),
            {"skill_id": skill_id},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def _create_revision(
        self,
        *,
        skill_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        version: int,
        files: SkillFiles,
    ) -> uuid.UUID:
        revision_hash = _content_hash(files, version=version)
        existing = (await self.session.execute(
            text(
                "SELECT revision_id FROM skill_revisions "
                "WHERE skill_id=:skill_id AND revision_hash=:revision_hash"
            ),
            {"skill_id": skill_id, "revision_hash": revision_hash},
        )).scalar_one_or_none()
        if existing is not None:
            revision_id = existing
        else:
            revision_id = uuid.uuid4()
            manifest, total, normalized = _manifest(files)
            await self.session.execute(
                text(
                    "INSERT INTO skill_revisions "
                    "(revision_id, skill_id, tenant_id, user_id, revision_hash, "
                    "version, file_manifest, size_bytes) "
                    "VALUES (:revision_id, :skill_id, :tenant_id, :user_id, "
                    ":revision_hash, :version, CAST(:manifest AS jsonb), :size_bytes)"
                ),
                {
                    "revision_id": revision_id,
                    "skill_id": skill_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "revision_hash": revision_hash,
                    "version": version,
                    "manifest": json.dumps(manifest),
                    "size_bytes": total,
                },
            )
            for path, media_type, data in normalized:
                envelope = await self._encrypt_file(
                    tenant_id=tenant_id,
                    skill_id=skill_id,
                    purpose="skill_revision_file",
                    record_id=f"{revision_id}:{path}",
                    data=data,
                )
                await self.session.execute(
                    text(
                        "INSERT INTO skill_revision_files "
                        "(revision_id, path, skill_id, tenant_id, user_id, "
                        "content_type, content_hash, size_bytes, "
                        "content_ciphertext, content_nonce, content_key_id) "
                        "VALUES (:revision_id, :path, :skill_id, :tenant_id, "
                        ":user_id, :content_type, :content_hash, :size_bytes, "
                        ":content_ciphertext, :content_nonce, :content_key_id)"
                    ),
                    {
                        "revision_id": revision_id,
                        "path": path,
                        "skill_id": skill_id,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "content_type": media_type,
                        "content_hash": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data),
                        **envelope,
                    },
                )
        await self.session.execute(
            text(
                "UPDATE skills SET current_revision_id=:revision_id, "
                "updated_at=now() WHERE skill_id=:skill_id"
            ),
            {"revision_id": revision_id, "skill_id": skill_id},
        )
        await self._project_latest(
            skill_id=skill_id,
            tenant_id=tenant_id,
            user_id=user_id,
            files=files,
        )
        return revision_id

    async def _project_latest(
        self,
        *,
        skill_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        files: SkillFiles,
    ) -> None:
        scope_id = skill_scope_id(user_id)
        prefix = f"/skills/{skill_id}"
        await self.vfs.delete_artifact(
            wf_id=scope_id,
            tenant=str(tenant_id),
            path=prefix,
        )
        for path, content_type, data in files:
            media_type = (
                content_type
                or mimetypes.guess_type(path)[0]
                or "application/octet-stream"
            )
            await self.vfs.upsert_internal_artifact_bytes(
                wf_id=scope_id,
                tenant=str(tenant_id),
                path=f"{prefix}/{path}",
                data=data,
                content_type=media_type,
                abstract=f"Latest published Skill file: {path}",
            )

    async def list_for_user(self, user_id: str | uuid.UUID) -> list[dict]:
        rows = (await self.session.execute(
            text(
                "SELECT s.*, EXISTS (SELECT 1 FROM user_skill_installations i "
                "WHERE i.skill_id=s.skill_id AND i.user_id=NULLIF(current_setting('app.user_id', true), '')::uuid) AS installed, "
                "r.revision_hash, r.file_manifest, "
                "r.size_bytes AS revision_size_bytes "
                "FROM skills s LEFT JOIN skill_revisions r "
                "ON r.revision_id=s.current_revision_id "
                "WHERE s.user_id=:user_id AND s.deleted_at IS NULL "
                "ORDER BY s.created_at DESC"
            ),
            {"user_id": user_id},
        )).mappings().all()
        return [dict(row) for row in rows]

    async def find_live_source(
        self,
        *,
        source: str,
        source_id: str,
    ) -> dict | None:
        row = (
            await self.session.execute(
                text(
                    "SELECT skill_id, source, source_id FROM skills "
                    "WHERE source=:source AND source_id=:source_id "
                    "AND deleted_at IS NULL LIMIT 1"
                ),
                {"source": source, "source_id": source_id},
            )
        ).mappings().one_or_none()
        return dict(row) if row else None

    async def list_authorized(
        self,
        authorized_skill_ids: Sequence[str],
    ) -> list[dict]:
        """SQL-intersect OpenFGA IDs with the active organization.

        The caller obtains opaque IDs from ``AuthzService.list_authorized_ids``.
        RLS remains the hard organization boundary and this query never falls
        back to returning the whole tenant for an empty/invalid ID collection.
        """
        parsed: list[uuid.UUID] = []
        for value in authorized_skill_ids:
            try:
                parsed.append(uuid.UUID(str(value)))
            except (TypeError, ValueError):
                continue
        if not parsed:
            return []
        rows = (
            await self.session.execute(
                text(
                    "SELECT s.*, EXISTS (SELECT 1 FROM user_skill_installations i "
                    "WHERE i.skill_id=s.skill_id AND i.user_id=NULLIF(current_setting('app.user_id', true), '')::uuid) AS installed, "
                    "r.revision_hash, r.file_manifest, "
                    "r.size_bytes AS revision_size_bytes "
                    "FROM skills s LEFT JOIN skill_revisions r "
                    "ON r.revision_id=s.current_revision_id "
                    "WHERE s.skill_id = ANY(CAST(:skill_ids AS uuid[])) "
                    "AND s.deleted_at IS NULL "
                    "ORDER BY s.created_at DESC"
                ),
                {"skill_ids": [str(value) for value in parsed]},
            )
        ).mappings().all()
        return [dict(row) for row in rows]

    async def get(
        self, skill_id: uuid.UUID | str, *, user_id: str | uuid.UUID | None = None,
    ) -> Optional[dict]:
        sql = (
            "SELECT s.*, EXISTS (SELECT 1 FROM user_skill_installations i "
            "WHERE i.skill_id=s.skill_id AND i.user_id=NULLIF(current_setting('app.user_id', true), '')::uuid) AS installed, "
            "r.revision_hash, r.file_manifest, "
            "r.size_bytes AS revision_size_bytes "
            "FROM skills s LEFT JOIN skill_revisions r "
            "ON r.revision_id=s.current_revision_id "
            "WHERE s.skill_id=:id AND s.deleted_at IS NULL"
        )
        params: dict = {"id": skill_id}
        if user_id is not None:
            sql += " AND s.user_id=:user_id"
            params["user_id"] = user_id
        row = (await self.session.execute(text(sql), params)).mappings().one_or_none()
        return dict(row) if row else None

    async def list_revisions(
        self,
        skill_id: uuid.UUID | str,
        *,
        user_id: str | uuid.UUID | None = None,
    ) -> list[dict]:
        user_clause = " AND r.user_id=:user_id" if user_id is not None else ""
        rows = (await self.session.execute(
            text(
                "SELECT r.revision_id, r.revision_hash, r.version, "
                "r.file_manifest, r.size_bytes, r.created_at, "
                "(r.revision_id=s.current_revision_id) AS is_latest "
                "FROM skill_revisions r JOIN skills s ON s.skill_id=r.skill_id "
                "WHERE r.skill_id=:skill_id" + user_clause + " "
                "AND s.deleted_at IS NULL ORDER BY r.version DESC, r.created_at DESC"
            ),
            {"skill_id": skill_id, **({"user_id": user_id} if user_id is not None else {})},
        )).mappings().all()
        return [dict(row) for row in rows]

    async def get_revision(
        self,
        skill_id: uuid.UUID | str,
        revision_id: uuid.UUID | str,
        *,
        user_id: str | uuid.UUID | None = None,
    ) -> Optional[dict]:
        user_clause = " AND r.user_id=:user_id" if user_id is not None else ""
        row = (await self.session.execute(
            text(
                "SELECT r.*, (r.revision_id=s.current_revision_id) AS is_latest "
                "FROM skill_revisions r JOIN skills s ON s.skill_id=r.skill_id "
                "WHERE r.skill_id=:skill_id AND r.revision_id=:revision_id "
                + user_clause + " AND s.deleted_at IS NULL"
            ),
            {
                "skill_id": skill_id,
                "revision_id": revision_id,
                **({"user_id": user_id} if user_id is not None else {}),
            },
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def soft_delete(
        self, skill_id: uuid.UUID, *, user_id: str | uuid.UUID | None = None,
    ) -> None:
        row = await self.get(skill_id, user_id=user_id)
        if row is None:
            return
        sql = (
            "UPDATE skills SET deleted_at=now(), updated_at=now() "
            "WHERE skill_id=:id AND deleted_at IS NULL"
        )
        params: dict = {"id": skill_id}
        if user_id is not None:
            sql += " AND user_id=:user_id"
            params["user_id"] = user_id
        await self.session.execute(text(sql), params)
        await self.vfs.delete_artifact(
            wf_id=skill_scope_id(row["user_id"]),
            tenant=str(row["tenant_id"]),
            path=f"/skills/{skill_id}",
        )

    async def _read_revision_files(self, revision_id: uuid.UUID | str) -> SkillFiles:
        rows = (await self.session.execute(
            text(
                "SELECT path, content_type, skill_id, tenant_id, "
                "content_ciphertext, content_nonce, content_key_id "
                "FROM skill_revision_files "
                "WHERE revision_id=:revision_id ORDER BY path"
            ),
            {"revision_id": revision_id},
        )).mappings().all()
        files: SkillFiles = []
        for row in rows:
            path = str(row["path"])
            files.append((
                path,
                str(row["content_type"]),
                await self._decrypt_file(
                    row,
                    purpose="skill_revision_file",
                    record_id=f"{revision_id}:{path}",
                ),
            ))
        return files

    async def read_current_files(
        self,
        skill_id: uuid.UUID | str,
        *,
        user_id: str | uuid.UUID | None = None,
    ) -> Optional[SkillFiles]:
        row = await self.get(skill_id, user_id=user_id)
        if row is None or row.get("current_revision_id") is None:
            return None
        return await self._read_revision_files(row["current_revision_id"])

    async def read_revision_files(
        self,
        skill_id: uuid.UUID | str,
        revision_id: uuid.UUID | str,
        *,
        user_id: str | uuid.UUID | None = None,
    ) -> Optional[SkillFiles]:
        row = await self.get_revision(skill_id, revision_id, user_id=user_id)
        return await self._read_revision_files(row["revision_id"]) if row else None

    async def get_draft(
        self,
        skill_id: uuid.UUID | str,
        *,
        user_id: str | uuid.UUID | None = None,
        for_update: bool = False,
    ) -> Optional[dict]:
        suffix = " FOR UPDATE" if for_update else ""
        user_clause = " AND d.user_id=:user_id" if user_id is not None else ""
        row = (await self.session.execute(
            text(
                "SELECT d.*, r.revision_hash AS base_revision_hash "
                "FROM skill_drafts d JOIN skill_revisions r "
                "ON r.revision_id=d.base_revision_id "
                "WHERE d.skill_id=:skill_id" + user_clause + suffix
            ),
            {"skill_id": skill_id, **({"user_id": user_id} if user_id is not None else {})},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def read_draft_files(
        self,
        skill_id: uuid.UUID | str,
        *,
        user_id: str | uuid.UUID | None = None,
    ) -> Optional[SkillFiles]:
        if await self.get_draft(skill_id, user_id=user_id) is None:
            return None
        user_clause = " AND user_id=:user_id" if user_id is not None else ""
        rows = (await self.session.execute(
            text(
                "SELECT path, content_type, skill_id, tenant_id, "
                "content_ciphertext, content_nonce, content_key_id "
                "FROM skill_draft_files "
                "WHERE skill_id=:skill_id" + user_clause + " ORDER BY path"
            ),
            {"skill_id": skill_id, **({"user_id": user_id} if user_id is not None else {})},
        )).mappings().all()
        files: SkillFiles = []
        for row in rows:
            path = str(row["path"])
            files.append((
                path,
                str(row["content_type"]),
                await self._decrypt_file(
                    row,
                    purpose="skill_draft_file",
                    record_id=f"{skill_id}:{path}",
                ),
            ))
        return files

    async def read_bundle_file(
        self, skill_id: uuid.UUID | str, path: str,
    ) -> Optional[bytes]:
        row = (await self.session.execute(
            text(
                "SELECT f.skill_id, f.tenant_id, f.content_ciphertext, "
                "f.content_nonce, f.content_key_id, f.revision_id, f.path "
                "FROM skills s JOIN skill_revision_files f "
                "ON f.revision_id=s.current_revision_id "
                "WHERE s.skill_id=:skill_id AND f.path=:path "
                "AND s.deleted_at IS NULL"
            ),
            {"skill_id": skill_id, "path": path},
        )).mappings().one_or_none()
        if row is None:
            return None
        return await self._decrypt_file(
            row,
            purpose="skill_revision_file",
            record_id=f"{row['revision_id']}:{row['path']}",
        )

    async def read_bundle_file_with_type(
        self, skill_id: uuid.UUID | str, path: str,
    ) -> Optional[tuple[bytes, str]]:
        row = (await self.session.execute(
            text(
                "SELECT f.skill_id, f.tenant_id, f.content_ciphertext, "
                "f.content_nonce, f.content_key_id, f.revision_id, f.path, "
                "f.content_type FROM skills s "
                "JOIN skill_revision_files f ON f.revision_id=s.current_revision_id "
                "WHERE s.skill_id=:skill_id AND f.path=:path "
                "AND s.deleted_at IS NULL"
            ),
            {"skill_id": skill_id, "path": path},
        )).mappings().one_or_none()
        if row is None:
            return None
        content = await self._decrypt_file(
            row,
            purpose="skill_revision_file",
            record_id=f"{row['revision_id']}:{row['path']}",
        )
        return content, str(row["content_type"])

    def purge_bundle(self, tenant_id, skill_id) -> None:
        # The caller's transaction rollback removes database files. VFS latest
        # projection is a replaceable cache and is repaired on the next publish.
        return None
