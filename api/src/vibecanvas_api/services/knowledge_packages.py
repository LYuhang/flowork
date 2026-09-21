"""Authoritative file-tree operations for versioned Knowledge packages.

Raw files are the source of truth. ``kb_chunks`` remain a derived lexical
projection that can be deleted and rebuilt without changing a package version.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import PurePosixPath
import stat
import uuid
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.services.background_queue import enqueue_background_job_async
from vibecanvas_api.services.file_format import content_type_for
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.parsers import detect_parser_type
from vibecanvas_api.services.queue_routing import route_for
from vibecanvas_api.storage.models_kb import KnowledgeBase
from vibecanvas_api.storage.repo_kb import KbRepo


MAX_PACKAGE_FILES = 256
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_PACKAGE_DEPTH = 16
MAX_PACKAGE_FILE_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PackageFile:
    path: str
    data: bytes
    content_type: str


def normalize_package_path(value: str) -> str:
    """Return a portable relative package path and reject traversal."""
    supplied = str(value or "").replace("\\", "/")
    raw = supplied.strip("/")
    path = PurePosixPath(raw)
    if (
        not raw
        or supplied.startswith("/")
        or "//" in supplied
        or any(ord(character) < 32 for character in supplied)
        or path.is_absolute()
        or len(path.parts) > MAX_PACKAGE_DEPTH
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"invalid Knowledge package path: {value!r}")
    return path.as_posix()


def validate_package(files: list[PackageFile]) -> list[PackageFile]:
    if not files:
        raise ValueError("Knowledge package must contain files")
    if len(files) > MAX_PACKAGE_FILES:
        raise ValueError(f"Knowledge package exceeds {MAX_PACKAGE_FILES} files")
    normalized: list[PackageFile] = []
    seen: set[str] = set()
    total = 0
    for item in files:
        path = normalize_package_path(item.path)
        folded = path.casefold()
        if folded in seen:
            raise ValueError(f"duplicate Knowledge package path: {path}")
        seen.add(folded)
        total += len(item.data)
        normalized.append(PackageFile(path, item.data, item.content_type))
    if "readme.md" not in seen:
        raise ValueError("Knowledge package root must contain README.md")
    if total > MAX_PACKAGE_BYTES:
        raise ValueError(
            f"Knowledge package exceeds {MAX_PACKAGE_BYTES} total bytes"
        )
    return normalized


def normalize_imported_package(files: list[PackageFile]) -> list[PackageFile]:
    """Validate an imported directory, accepting one transport-only wrapper.

    Browser directory uploads and archives commonly prefix every entry with
    the selected folder name.  That wrapper is not part of the Knowledge
    package: ``README.md`` must be at the logical package root after it is
    removed.  Arbitrary nested README files never satisfy the invariant.
    """
    if not files:
        return validate_package(files)
    normalized_paths = [normalize_package_path(item.path) for item in files]
    if any(path.casefold() == "readme.md" for path in normalized_paths):
        return validate_package(files)
    first_parts = {PurePosixPath(path).parts[0] for path in normalized_paths}
    if len(first_parts) != 1:
        raise ValueError("Knowledge package root must contain README.md")
    wrapper = next(iter(first_parts))
    prefix = f"{wrapper}/"
    unwrapped = [
        PackageFile(path[len(prefix):], item.data, item.content_type)
        for item, path in zip(files, normalized_paths, strict=True)
        if path.startswith(prefix)
    ]
    return validate_package(unwrapped)


def package_files_from_zip(blob: bytes) -> list[PackageFile]:
    """Read a bounded, regular-file-only ZIP into a validated package tree."""
    if len(blob) > MAX_PACKAGE_BYTES:
        raise ValueError("Knowledge ZIP exceeds the compressed upload limit")
    try:
        with ZipFile(BytesIO(blob)) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            if len(infos) > MAX_PACKAGE_FILES:
                raise ValueError(
                    f"Knowledge package exceeds {MAX_PACKAGE_FILES} files"
                )
            declared_total = 0
            package: list[PackageFile] = []
            for item in infos:
                if item.flag_bits & 0x1:
                    raise ValueError("Encrypted ZIP entries are not supported")
                unix_mode = item.external_attr >> 16
                if unix_mode and stat.S_IFMT(unix_mode) not in {0, stat.S_IFREG}:
                    raise ValueError("Knowledge ZIP may contain only regular files")
                if item.file_size > MAX_PACKAGE_FILE_BYTES:
                    raise ValueError(
                        f"Knowledge package file exceeds {MAX_PACKAGE_FILE_BYTES} bytes"
                    )
                declared_total += item.file_size
                if declared_total > MAX_PACKAGE_BYTES:
                    raise ValueError(
                        f"Knowledge package exceeds {MAX_PACKAGE_BYTES} total bytes"
                    )
                data = archive.read(item)
                if len(data) != item.file_size:
                    raise ValueError("Knowledge ZIP entry size changed while reading")
                package.append(PackageFile(item.filename, data, ""))
    except BadZipFile as exc:
        raise ValueError("Invalid Knowledge ZIP archive") from exc
    return normalize_imported_package(package)


def resolve_package_content_type(
    path: str,
    data: bytes,
    declared: str | None = None,
) -> str:
    """Resolve package MIME through the central file-format registry.

    Known extensions always use the platform's canonical type. For an unknown
    format, retain a meaningful caller-provided MIME rather than flattening it
    to the registry's text/binary fallback.
    """
    canonical = content_type_for(path, data)
    supplied = str(declared or "").strip().lower()
    if (
        canonical in {"text/plain", "application/octet-stream"}
        and supplied
        and supplied != "application/octet-stream"
    ):
        return supplied
    return canonical


async def package_snapshot(
    session: AsyncSession,
    kb_id: uuid.UUID,
) -> list[PackageFile]:
    repo = KbRepo(session)
    store = get_object_store()
    result: list[PackageFile] = []
    for file in await repo.list_files(kb_id):
        if not file.object_store_key:
            continue
        data = await asyncio.to_thread(store.fetch_bytes, file.object_store_key)
        result.append(PackageFile(file.name, data, file.mime_type))
    return result


async def replace_package(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    expected_version: int,
    files: list[PackageFile],
    increment_version: bool = True,
    derive_index: bool = True,
) -> tuple[int, list[uuid.UUID]]:
    """Replace one package tree under a row lock.

    The optimistic version check and metadata swap are transactional. Object
    writes use opaque, revision-specific keys, so a failed transaction cannot
    overwrite the prior authoritative package.
    """
    package = validate_package(files)
    kb = (
        await session.execute(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if kb is None:
        raise LookupError("knowledge_not_found")
    if kb.package_version != expected_version:
        raise RuntimeError(f"knowledge_version_conflict:{kb.package_version}")

    repo = KbRepo(session)
    for previous in await repo.list_files(kb_id):
        await repo.soft_delete_file(previous.id)
    await session.flush()

    next_version = expected_version + 1 if increment_version else expected_version
    store = get_object_store()
    pending: list[uuid.UUID] = []
    for item in package:
        content_type = resolve_package_content_type(
            item.path,
            item.data,
            item.content_type,
        )
        parser_type = detect_parser_type(item.path, content_type) or "binary"
        status = (
            "pending" if derive_index and parser_type != "binary" else "stored"
        )
        digest = hashlib.sha256(item.data).hexdigest()
        row = await repo.create_file(
            kb_id=kb.id,
            tenant_id=kb.tenant_id,
            user_id=actor_user_id,
            name=item.path,
            parser_type=parser_type,
            mime_type=content_type,
            file_size=len(item.data),
            content_hash=digest,
            status=status,
        )
        key = f"kb/{kb.tenant_id}/{kb.id}/v{next_version}/{row.id}/content"
        await asyncio.to_thread(store.put_bytes, key, item.data, content_type)
        await repo.set_object_store_key(row.id, key)
        if status == "pending":
            pending.append(row.id)
    kb.package_version = next_version
    await session.flush()
    return next_version, pending


async def enqueue_package_indexing(
    *, tenant_id: str, user_id: str, file_ids: list[uuid.UUID]
) -> None:
    for file_id in file_ids:
        task_id = uuid.uuid4()
        await enqueue_background_job_async(
            "kb.index_file",
            job_id=str(task_id),
            queue=route_for("kb_index_file"),
            kwargs={"file_id": str(file_id)},
        )


__all__ = [
    "MAX_PACKAGE_BYTES",
    "MAX_PACKAGE_DEPTH",
    "MAX_PACKAGE_FILES",
    "PackageFile",
    "enqueue_package_indexing",
    "MAX_PACKAGE_BYTES",
    "MAX_PACKAGE_FILE_BYTES",
    "MAX_PACKAGE_FILES",
    "normalize_package_path",
    "normalize_imported_package",
    "package_files_from_zip",
    "package_snapshot",
    "resolve_package_content_type",
    "replace_package",
    "validate_package",
]
