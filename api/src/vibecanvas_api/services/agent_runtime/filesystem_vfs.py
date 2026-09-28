"""Runtime-local VFS facade over the Chat workspace mounts.

The API host hydrates `/data`, `/memory`, `/logs`, and `/mount` before a turn
and writes them back after it.  Agent middleware can therefore use the legacy
sync VFS protocol without a platform database credential inside gVisor.
"""

from __future__ import annotations

from dataclasses import dataclass
import mimetypes
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


_ROOTS = ("/data", "/memory", "/logs", "/chats", "/mount")
_EXTENSIONS = {
    "table/jsonl": "jsonl",
    "table/csv": "csv",
    "table/tsv": "tsv",
    "table/xlsx": "xlsx",
    "text/plain": "txt",
    "text/python": "py",
    "text/html": "html",
    "application/json": "json",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "application/pdf": "pdf",
    "application/octet-stream": "bin",
}


@dataclass(slots=True)
class RuntimeVfsEntry:
    path: str
    kind: str
    content: str
    content_type: str
    abstract: str
    size_bytes: int
    wf_version: str | None
    last_access: float


def _content_type(path: Path, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _is_text(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type.startswith("table/")
        or content_type in {"application/json", "json", "text"}
    )


class FilesystemRuntimeVfsStore:
    """Synchronous, sandbox-scoped implementation of the VFS store protocol."""

    def __init__(self, roots: tuple[str, ...] = _ROOTS) -> None:
        self._roots = tuple(Path(root) for root in roots)

    def _resolve(self, path: str, *, for_write: bool = False) -> Path:
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise ValueError("VFS path must be absolute")
        parts = Path(path).parts
        if any(part in {".", ".."} for part in parts):
            raise ValueError("VFS path contains an invalid segment")
        root = next(
            (
                candidate
                for candidate in self._roots
                if path == str(candidate) or path.startswith(str(candidate) + "/")
            ),
            None,
        )
        if root is None:
            raise ValueError("VFS path is outside the Runtime workspace")
        root_real = root.resolve(strict=False)
        candidate = Path(path)
        check = candidate.parent.resolve(strict=False) if for_write else candidate.resolve(strict=False)
        try:
            check.relative_to(root_real)
        except ValueError as exc:
            raise ValueError("VFS path escapes the Runtime workspace") from exc
        if for_write and candidate == root:
            raise ValueError("VFS write path must name a file")
        return candidate

    def _write(self, path: str, data: bytes) -> bool:
        target = self._resolve(path, for_write=True)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        replaced = target.exists()
        fd, temporary = tempfile.mkstemp(prefix=".vc-write-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return replaced

    def read_bytes(self, *, wf_id: Any, path: str) -> bytes | None:
        del wf_id
        target = self._resolve(path)
        if not target.is_file():
            return None
        return target.read_bytes()

    def read(self, *, wf_id: Any, path: str) -> RuntimeVfsEntry | None:
        del wf_id
        target = self._resolve(path)
        if not target.is_file():
            return None
        stat = target.stat()
        content_type = _content_type(target)
        content = (
            target.read_text(encoding="utf-8", errors="replace")
            if _is_text(content_type)
            else ""
        )
        return RuntimeVfsEntry(
            path=path,
            kind="scratch" if path.startswith("/memory/") else "artifact",
            content=content,
            content_type=content_type,
            abstract="",
            size_bytes=stat.st_size,
            wf_version=None,
            last_access=stat.st_mtime,
        )

    def write_artifact(
        self,
        *,
        wf_id: Any,
        category: str,
        basename: str,
        content: str,
        content_type: str = "text/plain",
        wf_version: str | None = None,
        abstract: str = "",
    ) -> str:
        del wf_id, wf_version, abstract
        if category not in {"data", "memory", "logs", "chats", "mount"}:
            raise ValueError("unsupported Runtime VFS category")
        if not basename or "/" in basename or basename in {".", ".."}:
            raise ValueError("invalid Runtime VFS basename")
        extension = _EXTENSIONS.get(content_type, "txt")
        root = Path("/" + category)
        sequence = 1
        while (root / f"{basename}_{sequence}.{extension}").exists():
            sequence += 1
        path = str(root / f"{basename}_{sequence}.{extension}")
        self._write(path, content.encode("utf-8"))
        return path

    def write_artifact_bytes(
        self,
        *,
        wf_id: Any,
        category: str,
        basename: str,
        data: bytes,
        content_type: str,
        wf_version: str | None = None,
        abstract: str = "",
    ) -> str:
        del wf_id, wf_version, abstract
        if category not in {"data", "memory", "logs", "chats", "mount"}:
            raise ValueError("unsupported Runtime VFS category")
        if not basename or "/" in basename or basename in {".", ".."}:
            raise ValueError("invalid Runtime VFS basename")
        extension = _EXTENSIONS.get(content_type, "bin")
        root = Path("/" + category)
        sequence = 1
        while (root / f"{basename}_{sequence}.{extension}").exists():
            sequence += 1
        path = str(root / f"{basename}_{sequence}.{extension}")
        self._write(path, bytes(data))
        return path

    def write_scratch(
        self,
        *,
        wf_id: Any,
        path: str,
        content: str,
        content_type: str = "text/plain",
        abstract: str = "",
    ) -> None:
        del wf_id, content_type, abstract
        if not path.startswith("/memory/"):
            raise ValueError("scratch files must be under /memory")
        self._write(path, content.encode("utf-8"))

    def write_scratch_bytes(
        self,
        *,
        wf_id: Any,
        path: str,
        data: bytes,
        content_type: str,
        abstract: str = "",
    ) -> None:
        del wf_id, content_type, abstract
        if not path.startswith("/memory/"):
            raise ValueError("scratch files must be under /memory")
        self._write(path, bytes(data))

    def upsert_artifact(
        self,
        *,
        wf_id: Any,
        path: str,
        content: str,
        content_type: str = "text/plain",
        abstract: str = "",
    ) -> bool:
        del wf_id, content_type, abstract
        return self._write(path, content.encode("utf-8"))

    def upsert_artifact_bytes(
        self,
        *,
        wf_id: Any,
        path: str,
        data: bytes,
        content_type: str,
        abstract: str = "",
    ) -> bool:
        del wf_id, content_type, abstract
        return self._write(path, bytes(data))

    upsert_internal_artifact = upsert_artifact
    upsert_internal_artifact_bytes = upsert_artifact_bytes

    def delete_artifact(self, *, wf_id: Any, path: str) -> int:
        del wf_id
        target = self._resolve(path)
        if target.is_file() or target.is_symlink():
            target.unlink()
            return 1
        if target.is_dir():
            count = sum(1 for item in target.rglob("*") if item.is_file())
            shutil.rmtree(target)
            return count
        return 0

    def rename_artifact(
        self, *, wf_id: Any, old_path: str, new_path: str
    ) -> bool:
        del wf_id
        source = self._resolve(old_path)
        target = self._resolve(new_path, for_write=True)
        if not source.exists():
            raise ValueError("rename source not found")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(source, target)
        return True

    def ls(self, *, wf_id: Any, prefix: str) -> list[RuntimeVfsEntry]:
        del wf_id
        target = self._resolve(prefix)
        candidates: list[Path]
        if target.is_file():
            candidates = [target]
        elif target.is_dir():
            candidates = [item for item in target.rglob("*") if item.is_file()]
        else:
            root = self._resolve(str(Path(prefix).parent))
            candidates = [
                item
                for item in root.rglob("*")
                if item.is_file() and str(item).startswith(prefix)
            ]
        result = []
        for item in sorted(candidates):
            item_path = str(item)
            stat = item.stat()
            result.append(
                RuntimeVfsEntry(
                    path=item_path,
                    kind=(
                        "scratch" if item_path.startswith("/memory/") else "artifact"
                    ),
                    content="",
                    content_type=_content_type(item),
                    abstract="",
                    size_bytes=stat.st_size,
                    wf_version=None,
                    last_access=stat.st_mtime,
                )
            )
        return result

    def read_prefix_bytes(
        self, *, wf_id: Any, prefix: str
    ) -> list[tuple[RuntimeVfsEntry, bytes]]:
        return [
            (entry, self.read_bytes(wf_id=wf_id, path=entry.path) or b"")
            for entry in self.ls(wf_id=wf_id, prefix=prefix)
        ]

    def upsert_artifact_bytes_many(
        self,
        *,
        wf_id: Any,
        items: list[tuple[str, bytes, str]],
    ) -> int:
        for path, data, content_type in items:
            self.upsert_artifact_bytes(
                wf_id=wf_id,
                path=path,
                data=data,
                content_type=content_type,
            )
        return len(items)


__all__ = ["FilesystemRuntimeVfsStore", "RuntimeVfsEntry"]
