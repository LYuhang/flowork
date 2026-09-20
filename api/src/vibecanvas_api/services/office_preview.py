"""High-fidelity, read-only DOCX and PPTX Preview renditions.

Browser-side DOCX/PPTX parsers only approximate the native layout and can change
pagination, wrapping, and object placement.  This service converts a bounded
OOXML source to PDF with the same headless LibreOffice runtime used by Document
acceptance, then keeps a small process-local disk cache keyed by the source
bytes.  XLSX workbooks use the native spreadsheet renderer in the browser so
sheet navigation, cell formatting, and row/column geometry remain interactive.
The original file remains the downloadable source of truth.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading


SUPPORTED_OFFICE_PREVIEW_SUFFIXES = {".docx", ".pptx"}
OFFICE_PREVIEW_PDF_MAX_BYTES = 100 * 1024 * 1024
_CACHE_MAX_BYTES = 256 * 1024 * 1024
_CACHE_MAX_FILES = 64
_CACHE_ROOT = Path(tempfile.gettempdir()) / "flowork-office-preview"
_CACHE_LOCK = threading.RLock()
_CONVERSION_SLOTS = threading.BoundedSemaphore(2)


class OfficePreviewError(RuntimeError):
    """A deterministic Office rendition failure safe to map to an HTTP error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _office_command() -> str:
    command = shutil.which("libreoffice") or shutil.which("soffice")
    if not command:
        raise OfficePreviewError("office_renderer_unavailable")
    return command


def _cache_path(data: bytes, suffix: str) -> Path:
    digest = hashlib.sha256(suffix.encode("ascii") + b"\0" + data).hexdigest()
    return _CACHE_ROOT / f"{digest}.pdf"


def _read_cached(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if not data.startswith(b"%PDF-"):
        path.unlink(missing_ok=True)
        return None
    try:
        path.touch()
    except OSError:
        pass
    return data


def _prune_cache() -> None:
    entries = sorted(
        (path for path in _CACHE_ROOT.glob("*.pdf") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    total = 0
    for index, path in enumerate(entries):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        total += size
        if index >= _CACHE_MAX_FILES or total > _CACHE_MAX_BYTES:
            path.unlink(missing_ok=True)


def render_office_preview_pdf(data: bytes, suffix: str) -> bytes:
    """Convert one bounded DOCX/PPTX payload to a faithful PDF rendition."""

    normalized_suffix = suffix.lower()
    if normalized_suffix not in SUPPORTED_OFFICE_PREVIEW_SUFFIXES:
        raise OfficePreviewError("unsupported_office_preview_type")
    if not data:
        raise OfficePreviewError("invalid_office_file")

    cache_path = _cache_path(data, normalized_suffix)
    with _CACHE_LOCK:
        _CACHE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(_CACHE_ROOT, 0o700)
        cached = _read_cached(cache_path)
        if cached is not None:
            return cached

    with _CONVERSION_SLOTS, tempfile.TemporaryDirectory(
        prefix="flowork-office-preview-",
    ) as temporary:
        temporary_path = Path(temporary)
        source = temporary_path / f"source{normalized_suffix}"
        source.write_bytes(data)
        profile = temporary_path / "profile"
        environment = {
            **os.environ,
            "TMPDIR": str(temporary_path),
        }
        try:
            completed = subprocess.run(
                [
                    _office_command(),
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nolockcheck",
                    "--norestore",
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(temporary_path),
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise OfficePreviewError("office_preview_timeout") from exc
        rendered = source.with_suffix(".pdf")
        if completed.returncode != 0 or not rendered.is_file():
            raise OfficePreviewError("office_preview_conversion_failed")
        pdf = rendered.read_bytes()
        if not pdf.startswith(b"%PDF-"):
            raise OfficePreviewError("office_preview_invalid_pdf")
        if len(pdf) > OFFICE_PREVIEW_PDF_MAX_BYTES:
            raise OfficePreviewError("office_preview_too_large")

    with _CACHE_LOCK:
        temporary_cache = cache_path.with_name(
            f".{cache_path.stem}-{os.getpid()}-{threading.get_ident()}.tmp"
        )
        temporary_cache.write_bytes(pdf)
        os.chmod(temporary_cache, 0o600)
        os.replace(temporary_cache, cache_path)
        _prune_cache()
    return pdf


__all__ = [
    "OFFICE_PREVIEW_PDF_MAX_BYTES",
    "OfficePreviewError",
    "SUPPORTED_OFFICE_PREVIEW_SUFFIXES",
    "render_office_preview_pdf",
]
