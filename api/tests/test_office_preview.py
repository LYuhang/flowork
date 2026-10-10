from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vibecanvas_api.services import office_preview


def test_office_preview_converts_once_and_reuses_bounded_disk_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(office_preview, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(office_preview.shutil, "which", lambda _name: "/usr/bin/soffice")

    def fake_run(arguments, **_kwargs):
        calls.append(arguments)
        source = Path(arguments[-1])
        source.with_suffix(".pdf").write_bytes(b"%PDF-1.7\nrendered")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(office_preview.subprocess, "run", fake_run)
    source = b"PK\x03\x04office"

    first = office_preview.render_office_preview_pdf(source, ".docx")
    second = office_preview.render_office_preview_pdf(source, ".docx")

    assert first == second == b"%PDF-1.7\nrendered"
    assert len(calls) == 1
    assert "--headless" in calls[0]
    assert list((tmp_path / "cache").glob("*.pdf"))


def test_office_preview_rejects_unsupported_source_type() -> None:
    with pytest.raises(
        office_preview.OfficePreviewError,
        match="unsupported_office_preview_type",
    ):
        office_preview.render_office_preview_pdf(b"content", ".xls")


def test_concurrent_identical_previews_share_one_conversion(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    monkeypatch.setattr(office_preview, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(office_preview.shutil, "which", lambda _name: "/usr/bin/soffice")
    started, release = threading.Event(), threading.Event()
    calls = []

    def convert(arguments, **_kwargs):
        calls.append(arguments)
        started.set()
        assert release.wait(5)
        Path(arguments[-1]).with_suffix(".pdf").write_bytes(b"%PDF-1.7\nshared")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(office_preview.subprocess, "run", convert)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(office_preview.render_office_preview_pdf, b"same", ".docx") for _ in range(5)]
        assert started.wait(5)
        release.set()
        assert [f.result(timeout=5) for f in futures] == [b"%PDF-1.7\nshared"] * 5
    assert len(calls) == 1


def test_failed_conversion_releases_source_and_conversion_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(office_preview, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(office_preview.shutil, "which", lambda _name: "/usr/bin/soffice")
    calls = []

    def convert(arguments, **_kwargs):
        calls.append(arguments)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="failure")
        Path(arguments[-1]).with_suffix(".pdf").write_bytes(b"%PDF-1.7\nrecovered")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(office_preview.subprocess, "run", convert)
    with pytest.raises(office_preview.OfficePreviewError):
        office_preview.render_office_preview_pdf(b"same", ".docx")
    assert office_preview.render_office_preview_pdf(b"same", ".docx") == b"%PDF-1.7\nrecovered"
