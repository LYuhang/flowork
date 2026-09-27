"""Sandbox-local trusted Diagram CLI execution."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from vibecanvas_api.document_runtime.review import _resolve_workspace_file
from vibecanvas_api.document_runtime.rendering import _output_directory, _pages
from vibecanvas_api.drawio import inspect_drawio

FEEDBACK_ROOT = Path("/memory/diagram-feedback")


def _required_command(name):
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"Diagram runtime command '{name}' is unavailable or not executable. Ask the platform operator to check the Diagram runtime installation.")
    return executable


def review_bytes(path, data):
    inspection = inspect_drawio(data)
    return {"status": "passed" if inspection.valid else "failed", "file": str(path),
            "format": "drawio", "source_hash": inspection.source_hash,
            "errors": list(inspection.issues), "warnings": list(inspection.warnings),
            "details": {"page_count": inspection.pages, "pages": list(inspection.page_details),
                        "cells": inspection.cells, "vertices": inspection.vertices, "edges": inspection.edges},
            "message": "Structural checks passed. Render and inspect every page before publishing." if inspection.valid else "Fix the reported page/cell errors, then review again."}


def review_diagram(file):
    path = _resolve_workspace_file(file)
    return review_bytes(path, path.read_bytes())


def search_shapes(query, limit=10, progress=lambda _: None):
    progress({"status": "running", "stage": "searching", "query": query})
    completed = subprocess.run([_required_command("flowork-diagram-search")], input=json.dumps({"query": query, "limit": limit}), capture_output=True, text=True)
    try:
        result = json.loads(completed.stdout)
        if not isinstance(result, dict) or result.get("status") not in {"succeeded", "failed"}:
            raise ValueError("Invalid shape-search response.")
        return result
    except ValueError as exc:
        raise RuntimeError("Official shape search failed. Check the installed runtime and network access. " + completed.stderr[-500:]) from exc


def render_diagram(file, *, pages=None, output_dir=None, progress=lambda _: None):
    source = _resolve_workspace_file(file)
    data = source.read_bytes()
    review = review_bytes(source, data)
    result = {"status": "failed", "file": str(source), "source_hash": review["source_hash"],
              "total_pages": review["details"]["page_count"], "rendered_pages": 0,
              "complete": False, "output_dir": None, "images": [], "_image_hashes": {}}
    if review["status"] != "passed":
        return {**result, "error": "invalid_diagram", "errors": review["errors"], "message": "The diagram failed structural checks.", "hint": "Run flowork-cli diagram review --file PATH, fix its errors, then render again."}
    try:
        total = result["total_pages"]
        selected = _pages(pages, total)
        if output_dir is None:
            FEEDBACK_ROOT.mkdir(parents=True, exist_ok=True)
            destination = Path(tempfile.mkdtemp(prefix="review-", dir=FEEDBACK_ROOT))
        else:
            destination = _output_directory(output_dir)
        result["output_dir"] = str(destination)
        export = _required_command("flowork-drawio-export")
        progress({"status": "running", "stage": "rendering", "file": str(source), "source_hash": result["source_hash"], "total_pages": total, "selected_pages": len(selected)})
        with tempfile.TemporaryDirectory(prefix="flowork-diagram-") as temporary:
            work = Path(temporary)
            snapshot = work / "source.drawio"
            snapshot.write_bytes(data)
            for number in selected:
                image = work / f"page-{number:03d}.png"
                # Pinned Desktop 31.1.8 uses 1-based indices (since 27.0.2).
                process = subprocess.run([export, "--export", "--format", "png", "--page-index", str(number), "--output", str(image), str(snapshot)], capture_output=True, text=True)
                if process.returncode or not image.is_file():
                    raise RuntimeError(f"Official draw.io export failed on page {number}: " + (process.stderr or process.stdout)[-1000:].strip())
                with image.open("rb") as stream:
                    if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                        raise RuntimeError(f"Official renderer did not return PNG data for page {number}.")
                target = destination / image.name
                shutil.move(str(image), str(target))
                result["_image_hashes"][str(target)] = hashlib.sha256(target.read_bytes()).hexdigest()
                name = review["details"]["pages"][number - 1]["name"]
                result["images"].append({"page": number, "name": name, "file": str(target)})
                result["rendered_pages"] += 1
                progress({"status": "running", "page": number, "name": name, "file": str(target), "rendered_pages": result["rendered_pages"], "selected_pages": len(selected), "total_pages": total})
        result.update(status="succeeded", complete=len(selected) == total,
                      message="Rendering completed. Open every PNG with the native image tool, then publish the accepted .drawio file with render_preview.")
    except Exception as exc:
        invalid = isinstance(exc, ValueError)
        result.update(error="invalid_arguments" if invalid else "render_failed", message=str(exc),
                      hint="Correct the arguments; use a new --output_dir or omit it. See flowork-cli diagram render --help." if invalid else "Any completed images are retained. Resolve the renderer failure and render missing pages of this source revision; do not claim full visual acceptance.")
    return result
