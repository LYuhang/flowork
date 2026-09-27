#!/usr/bin/env python3
"""Backport CPython gh-157190 / CVE-2026-82049 to the pinned 3.11 runtime."""

from __future__ import annotations

import pathlib
import sys
import sysconfig

VULNERABLE = "                    os.link(tarinfo._link_target, targetpath)\n"
PATCHED = "                    os.link(os.path.realpath(tarinfo._link_target), targetpath)\n"


def main() -> int:
    if sys.version_info[:3] != (3, 11, 16):
        raise RuntimeError(f"expected Python 3.11.16, got {sys.version.split()[0]}")
    target = pathlib.Path(sysconfig.get_path("stdlib")) / "tarfile.py"
    source = target.read_text(encoding="utf-8")
    if source.count(PATCHED) == 1 and VULNERABLE not in source:
        print(f"python_tarfile_backport=already-applied path={target}")
        return 0
    if source.count(VULNERABLE) != 1 or PATCHED in source:
        raise RuntimeError(f"unexpected tarfile source; refusing to patch {target}")
    target.write_text(source.replace(VULNERABLE, PATCHED), encoding="utf-8")
    print(f"python_tarfile_backport=applied path={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
