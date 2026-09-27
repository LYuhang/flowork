"""Snapshot-based, incremental Office/PDF rendering for the Document CLI."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .review import DocumentReviewError, _resolve_workspace_file, _WORKSPACE_ROOTS

_OFFICE_TYPES = {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}
_FEEDBACK_ROOT = Path("/memory/document-feedback")


def _required_command(*names):
    for name in names:
        if command := shutil.which(name):
            return command
    raise DocumentReviewError("Document renderer is unavailable in this environment.")


def _run(arguments, **kwargs):
    # The owning runtime kills the worker's process group on cancellation.
    # No total conversion/rendering timeout.
    completed = subprocess.run(arguments, capture_output=True, text=True, check=False, **kwargs)
    if completed.returncode:
        raise DocumentReviewError("Document rendering failed: " + (completed.stderr or completed.stdout or "Renderer exited unsuccessfully.")[-1000:].strip())


def _pages(selection, total):
    if selection is None:
        return list(range(1, total + 1))
    from vibecanvas_api.flowork_cli.document_cli import validate
    validate("document.render", {"file": "/data/document.pdf", "pages": selection})
    result = set()
    for part in selection.split(","):
        bounds = [int(value) for value in part.split("-")]
        start, end = bounds[0], bounds[-1]
        if end > total:
            raise ValueError(f"Page {end} exceeds the document's {total} pages.")
        result.update(range(start, end + 1))
    return sorted(result)


def _output_directory(value):
    if value is None:
        _FEEDBACK_ROOT.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="review-", dir=_FEEDBACK_ROOT))
    path = Path(value)
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Output parent directory must already exist.") from exc
    if not any(parent == Path(root).resolve() or Path(root).resolve() in parent.parents for root in _WORKSPACE_ROOTS):
        raise ValueError("Output directory must be inside the sandbox workspace.")
    destination = parent / path.name
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError("Output directory already exists. Choose a new --output_dir or omit it for a unique directory.") from exc
    return destination


def render_document(path, *, dpi=144, pages=None, output_dir=None, progress=lambda _: None):
    from pypdf import PdfReader

    if type(dpi) is not int or not 96 <= dpi <= 220:
        raise ValueError("--dpi must be between 96 and 220.")
    source = _resolve_workspace_file(path)
    suffix = source.suffix.lower()
    if suffix not in _OFFICE_TYPES | {".pdf"}:
        raise DocumentReviewError("Rendering supports DOCX, PPTX, XLSX, PDF, ODT, ODP and ODS.")
    result = {"status": "failed", "file": str(source), "source_hash": None,
              "total_pages": 0, "rendered_pages": 0, "complete": False,
              "output_dir": None, "images": [], "dpi": dpi, "_image_hashes": {}}
    with tempfile.TemporaryDirectory(prefix="flowork-document-") as temporary:
        work = Path(temporary)
        snapshot = work / ("source" + suffix)
        digest = hashlib.sha256()
        with source.open("rb") as incoming, snapshot.open("xb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                digest.update(chunk)
                outgoing.write(chunk)
        result["source_hash"] = "sha256:" + digest.hexdigest()
        try:
            progress({"status": "running", "stage": "rendering" if suffix == ".pdf" else "converting", "file": str(source), "source_hash": result["source_hash"]})
            if suffix == ".pdf":
                pdf = snapshot
            else:
                _run([_required_command("libreoffice", "soffice"), "--headless", "--nologo", "--nodefault", "--nolockcheck", "--norestore",
                      f"-env:UserInstallation={(work / 'profile').as_uri()}", "--convert-to", "pdf", "--outdir", str(work), str(snapshot)],
                     env={**os.environ, "TMPDIR": str(work)})
                pdf = work / "source.pdf"
                if not pdf.is_file():
                    raise DocumentReviewError("Office conversion did not produce a PDF.")
            total = len(PdfReader(str(pdf)).pages)
            result["total_pages"] = total
            if not total:
                raise DocumentReviewError("Document contains no pages.")
            selected = _pages(pages, total)
            destination = _output_directory(output_dir)
            result["output_dir"] = str(destination)
            rasterizer = _required_command("pdftoppm")
            for number in selected:
                prefix = work / f"page-{number:03d}"
                _run([rasterizer, "-png", "-r", str(dpi), "-f", str(number), "-l", str(number), "-singlefile", str(pdf), str(prefix)])
                image = prefix.with_suffix(".png")
                target = destination / image.name
                shutil.move(str(image), str(target))
                result["_image_hashes"][str(target)] = hashlib.sha256(target.read_bytes()).hexdigest()
                result["images"].append({"page": number, "file": str(target)})
                result["rendered_pages"] += 1
                progress({"status": "running", "page": number, "file": str(target), "rendered_pages": result["rendered_pages"], "selected_pages": len(selected), "total_pages": total})
            result.update(status="succeeded", complete=len(selected) == total,
                          message="Rendering completed. Inspect every image before publishing the native file.")
        except Exception as exc:
            invalid = isinstance(exc, ValueError) and not isinstance(exc, DocumentReviewError)
            result.update(error="invalid_arguments" if invalid else "render_failed", message=str(exc),
                          hint="Correct the arguments and retry; use a new --output_dir or omit it. See flowork-cli document render --help." if invalid else "Any completed images are retained. Correct the cause and render missing pages of the same source revision; do not claim full-document acceptance.")
    return result
