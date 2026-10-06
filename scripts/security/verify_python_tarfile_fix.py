#!/usr/bin/env python3
"""Regression probe for CPython #157190 using only a disposable directory."""

from __future__ import annotations

import io
import pathlib
import sys
import tarfile
import tempfile


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="flowork-tarfile-cve-") as name:
        base = pathlib.Path(name)
        outside = base / "outside"
        outside.write_text("outside-sentinel", encoding="utf-8")
        destination = base / "extract"
        destination.mkdir()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            inside = tarfile.TarInfo("outside")
            payload = b"inside-sentinel"
            inside.size = len(payload)
            archive.addfile(inside, io.BytesIO(payload))
            symlink = tarfile.TarInfo("deep/link")
            symlink.type = tarfile.SYMTYPE
            symlink.linkname = "../outside"
            archive.addfile(symlink)
            hardlink = tarfile.TarInfo("hardlink")
            hardlink.type = tarfile.LNKTYPE
            hardlink.linkname = "deep/link"
            archive.addfile(hardlink)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive:
            archive.extractall(destination, filter="data")
        actual = (destination / "hardlink").read_text(encoding="utf-8")
        if actual != "inside-sentinel":
            raise RuntimeError("CVE-2026-82049 regression: hard link escaped extraction root")
        if outside.read_text(encoding="utf-8") != "outside-sentinel":
            raise RuntimeError("test-owned outside sentinel changed")
    print(f"python_tarfile_regression=pass python={sys.version.split()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
