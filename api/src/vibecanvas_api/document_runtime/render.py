"""Native Office/PDF rendering used for Agent visual feedback."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .review import DocumentReviewError, _resolve_workspace_file


_OFFICE_TYPES = {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}
_FEEDBACK_ROOT = Path("/memory/document-feedback")


def _required_command(*names: str) -> str:
    for name in names:
        command = shutil.which(name)
        if command:
            return command
    raise DocumentReviewError(
        "document feedback renderer is unavailable in this environment"
    )


def render_document_feedback(
    path: str,
    *,
    dpi: int = 144,
    max_pages: int = 8,
) -> dict[str, Any]:
    """Render a native document to revision-bound PNG pages under ``/memory``."""

    if not 96 <= int(dpi) <= 220:
        raise DocumentReviewError("dpi must be between 96 and 220")
    if not 1 <= int(max_pages) <= 20:
        raise DocumentReviewError("max_pages must be between 1 and 20")
    source = _resolve_workspace_file(path)
    suffix = source.suffix.lower()
    if suffix not in _OFFICE_TYPES | {".pdf"}:
        raise DocumentReviewError(
            "visual feedback supports DOCX, PPTX, XLSX, ODT, ODP, ODS, and PDF"
        )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    pdftoppm = _required_command("pdftoppm")
    with tempfile.TemporaryDirectory(prefix="flowork-document-") as temporary:
        temporary_path = Path(temporary)
        if suffix == ".pdf":
            pdf = source
        else:
            office = _required_command("libreoffice", "soffice")
            profile = temporary_path / "profile"
            environment = {
                **os.environ,
                "HOME": str(temporary_path),
                "TMPDIR": str(temporary_path),
            }
            completed = subprocess.run(
                [
                    office,
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
            pdf = temporary_path / f"{source.stem}.pdf"
            if completed.returncode != 0 or not pdf.is_file():
                detail = (completed.stderr or completed.stdout or "").strip()
                raise DocumentReviewError(
                    "Office renderer could not create PDF feedback"
                    + (f": {detail[:500]}" if detail else "")
                )
        prefix = temporary_path / "page"
        # Ask for one page beyond the public limit so ``truncated`` describes
        # actual omitted output rather than merely "the limit was reached".
        completed = subprocess.run(
            [
                pdftoppm,
                "-png",
                "-r",
                str(int(dpi)),
                "-f",
                "1",
                "-l",
                str(int(max_pages) + 1),
                str(pdf),
                str(prefix),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        generated_all = sorted(temporary_path.glob("page-*.png"))
        if completed.returncode != 0 or not generated_all:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise DocumentReviewError(
                "PDF rasterizer could not create PNG feedback"
                + (f": {detail[:500]}" if detail else "")
            )
        safe_stem = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in source.stem
        ).strip("-")[:48] or "document"
        truncated = len(generated_all) > int(max_pages)
        generated = generated_all[: int(max_pages)]
        feedback_root = _FEEDBACK_ROOT / (
            f"{safe_stem}-{source_hash[:16]}"
        )
        feedback_root.mkdir(parents=True, exist_ok=True)
        feedback_paths = []
        for index, generated_page in enumerate(generated, start=1):
            destination = feedback_root / f"page-{index:03d}.png"
            shutil.copyfile(generated_page, destination)
            feedback_paths.append(str(destination))
    return {
        "path": path,
        "source_hash": f"sha256:{source_hash}",
        "feedback_paths": feedback_paths,
        "rendered_pages": len(feedback_paths),
        "truncated": truncated,
        "dpi": int(dpi),
    }


__all__ = ["render_document_feedback"]
