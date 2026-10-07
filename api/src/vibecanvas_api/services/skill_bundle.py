"""Validation and extraction for user-provided Skill bundles."""
from __future__ import annotations

import io
import posixpath
import stat
import zipfile
import zlib

from vibecanvas_api.config import config
from vibecanvas_api.services.skill_loader import SkillParseError, parse_skill_md


def _safe_path(raw: str) -> str:
    path = raw.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    normalized = posixpath.normpath(path)
    if (
        not normalized
        or normalized.startswith("/")
        or normalized == ".."
        or normalized.startswith("../")
        or any(segment in {".", ".."} for segment in path.split("/"))
        or "\x00" in normalized
    ):
        raise SkillParseError(f"unsafe Skill bundle path: {raw!r}")
    return normalized


def validate_skill_files(
    files: list[tuple[str, str | None, bytes]],
) -> tuple[dict, list[tuple[str, str | None, bytes]]]:
    if not files:
        raise SkillParseError("Skill bundle is empty")
    if len(files) > config.skills.max_files:
        raise SkillParseError(
            f"Skill bundle has {len(files)} files; limit is {config.skills.max_files}"
        )
    normalized = []
    seen = set()
    total = 0
    for raw_path, content_type, data in files:
        path = _safe_path(raw_path)
        if path in seen:
            raise SkillParseError(f"duplicate Skill bundle path: {path}")
        if len(data) > config.skills.max_file_bytes:
            raise SkillParseError(
                f"Skill file {path!r} exceeds {config.skills.max_file_bytes} bytes"
            )
        seen.add(path)
        total += len(data)
        normalized.append((path, content_type, data))
    if total > config.skills.max_bundle_bytes:
        raise SkillParseError(
            f"Skill bundle exceeds {config.skills.max_bundle_bytes} bytes"
        )
    skill_md = next((data for path, _ct, data in normalized if path == "SKILL.md"), None)
    if skill_md is None:
        raise SkillParseError("Skill bundle must contain SKILL.md at its root")
    try:
        text = skill_md.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillParseError("SKILL.md must be UTF-8 text") from exc
    frontmatter, _body = parse_skill_md(text)
    return frontmatter, normalized


def unpack_skill_zip(data: bytes) -> tuple[dict, list[tuple[str, str | None, bytes]]]:
    limit = config.skills.max_bundle_bytes
    if len(data) > limit:
        raise SkillParseError(f"compressed Skill bundle exceeds {limit} bytes")
    files: list[tuple[str, str | None, bytes]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > config.skills.max_files:
                raise SkillParseError(
                    f"Skill bundle has {len(entries)} entries; limit is {config.skills.max_files}"
                )
            total = 0
            seen = set()
            selected = []
            # Validate the entire directory before decompressing any entry.
            for info in entries:
                path = _safe_path(info.filename)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                    raise SkillParseError(
                        f"Skill bundle contains an unsupported link/device: {info.filename}"
                    )
                if info.flag_bits & 1:
                    raise SkillParseError("encrypted Skill bundles are not supported")
                if info.is_dir():
                    continue
                if path in seen:
                    raise SkillParseError(f"duplicate Skill bundle path: {path}")
                seen.add(path)
                if info.file_size > config.skills.max_file_bytes:
                    raise SkillParseError(
                        f"Skill file {path!r} exceeds {config.skills.max_file_bytes} bytes"
                    )
                total += info.file_size
                if total > limit:
                    raise SkillParseError(f"Skill bundle exceeds {limit} bytes")
                selected.append((path, info))
            total = 0
            for path, info in selected:
                allowed = min(config.skills.max_file_bytes, limit - total)
                with archive.open(info) as source:
                    content = source.read(allowed + 1)
                if len(content) > allowed:
                    raise SkillParseError(f"Skill bundle exceeds extraction limit at {path!r}")
                total += len(content)
                files.append((path, None, content))
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise SkillParseError("uploaded bundle is not a valid supported ZIP archive") from exc
    return validate_skill_files(files)
