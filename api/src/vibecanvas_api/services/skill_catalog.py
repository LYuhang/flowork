"""Read-only catalogs for installable Agent Skill bundles.

Only the repositories declared in ``_SOURCES`` are reachable. User input is a
source key and a catalog-relative skill id, never a URL, so this service cannot
be used as a general-purpose HTTP proxy.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from vibecanvas_api.services.skill_loader import SkillParseError, parse_skill_md


SkillCatalogSource = Literal["openai", "anthropic"]


@dataclass(frozen=True)
class _Source:
    owner: str
    repo: str
    ref: str
    prefix: str
    label: str


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    value: Any


_SOURCES: dict[SkillCatalogSource, _Source] = {
    "openai": _Source(
        owner="openai",
        repo="skills",
        ref="main",
        prefix="skills/.curated",
        label="OpenAI Curated Skills",
    ),
    "anthropic": _Source(
        owner="anthropics",
        repo="skills",
        ref="main",
        prefix="skills",
        label="Anthropic Agent Skills",
    ),
}
_CACHE_TTL_S = 300.0
_MAX_FILES = 500
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_BUNDLE_BYTES = 20 * 1024 * 1024
_USER_AGENT = "flowork/1.0 Skill catalog client"
_REQUEST_TIMEOUT_S = 15.0
_CATALOG_OPERATION_TIMEOUT_S = 22.0

_cache: dict[tuple[str, ...], _CacheEntry] = {}
_cache_lock = asyncio.Lock()


def _source(source: str) -> _Source:
    try:
        return _SOURCES[source]  # type: ignore[index]
    except KeyError as exc:
        raise ValueError(f"unsupported Skill catalog source: {source}") from exc


def _clean_source_id(source_id: str) -> str:
    value = source_id.strip().strip("/")
    if not value or value.startswith(".") or ".." in value.split("/"):
        raise ValueError("invalid Skill catalog id")
    return value


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        # TLS negotiation through desktop VPN/NAT layers can legitimately take
        # more than five seconds even when the TCP connect itself is fast.
        timeout=httpx.Timeout(_REQUEST_TIMEOUT_S, connect=10.0),
        follow_redirects=True,
        headers=_headers(),
    )


async def _fetch_json(
    url: str, *, client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if client is None:
        async with _http_client() as owned_client:
            return await _fetch_json(url, client=owned_client)
    response = await client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Skill catalog returned a non-object response")
    return payload


async def _fetch_bytes(
    url: str, *, client: httpx.AsyncClient | None = None,
) -> bytes:
    if client is None:
        async with _http_client() as owned_client:
            return await _fetch_bytes(url, client=owned_client)
    response = await client.get(url)
    response.raise_for_status()
    data = response.content
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError(f"Skill file exceeds {_MAX_FILE_BYTES} bytes")
    return data


def _raw_url(source: _Source, path: str) -> str:
    safe_path = quote(path, safe="/")
    return (
        f"https://raw.githubusercontent.com/{source.owner}/{source.repo}/"
        f"{source.ref}/{safe_path}"
    )


async def _tree(
    source_name: SkillCatalogSource, *, client: httpx.AsyncClient | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    cache_key = ("tree", source_name)
    cached = _cache.get(cache_key)
    now = time.monotonic()
    if cached and cached.expires_at > now:
        return cached.value

    source = _source(source_name)
    payload = await _fetch_json(
        f"https://api.github.com/repos/{source.owner}/{source.repo}/git/trees/"
        f"{source.ref}?recursive=1",
        client=client,
    )
    if payload.get("truncated"):
        raise ValueError(f"{source.label} file index is too large to inspect safely")
    rows = payload.get("tree") if isinstance(payload.get("tree"), list) else []
    entries = [row for row in rows if isinstance(row, dict)]
    result = (str(payload.get("sha") or source.ref), entries)
    async with _cache_lock:
        _cache[cache_key] = _CacheEntry(now + _CACHE_TTL_S, result)
    return result


def _skill_md_entries(source: _Source, entries: list[dict[str, Any]]) -> list[str]:
    prefix = f"{source.prefix}/"
    return sorted(
        str(entry["path"])
        for entry in entries
        if entry.get("type") == "blob"
        and isinstance(entry.get("path"), str)
        and str(entry["path"]).startswith(prefix)
        and str(entry["path"]).endswith("/SKILL.md")
    )


def _item_from_skill_md(
    *, source_name: SkillCatalogSource, source: _Source, revision: str,
    path: str, raw: bytes, entries: list[dict[str, Any]],
) -> dict[str, Any]:
    text = raw.decode("utf-8")
    frontmatter, body = parse_skill_md(text)
    skill_dir = path.removesuffix("/SKILL.md")
    source_id = skill_dir.removeprefix(f"{source.prefix}/")
    bundle_prefix = f"{skill_dir}/"
    files = [
        {
            "path": str(entry["path"])[len(bundle_prefix):],
            "size_bytes": int(entry.get("size") or 0),
        }
        for entry in entries
        if entry.get("type") == "blob"
        and isinstance(entry.get("path"), str)
        and str(entry["path"]).startswith(bundle_prefix)
    ]
    version = frontmatter.get("version", 1)
    return {
        "source": source_name,
        "source_label": source.label,
        "source_id": source_id,
        "name": str(frontmatter["name"]).strip(),
        "description": str(frontmatter["description"]).strip(),
        "version": version if isinstance(version, int) else 1,
        "allowed_tools": list(frontmatter.get("allowed_tools") or []),
        "homepage": f"https://github.com/{source.owner}/{source.repo}/tree/{source.ref}/{skill_dir}",
        "revision": revision,
        "files": files,
        "skill_md": text,
        "body": body,
    }


async def resolve_skill_catalog_item(
    *, source: SkillCatalogSource, source_id: str,
    _client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    clean_id = _clean_source_id(source_id)
    cache_key = ("item", source, clean_id)
    cached = _cache.get(cache_key)
    now = time.monotonic()
    if cached and cached.expires_at > now:
        return cached.value

    source_meta = _source(source)
    revision, entries = await _tree(source, client=_client)
    path = f"{source_meta.prefix}/{clean_id}/SKILL.md"
    valid_paths = set(_skill_md_entries(source_meta, entries))
    if path not in valid_paths:
        raise LookupError("Skill was not found in this catalog")
    raw = await _fetch_bytes(_raw_url(source_meta, path), client=_client)
    item = _item_from_skill_md(
        source_name=source,
        source=source_meta,
        revision=revision,
        path=path,
        raw=raw,
        entries=entries,
    )
    async with _cache_lock:
        _cache[cache_key] = _CacheEntry(now + _CACHE_TTL_S, item)
    return item


async def search_skill_catalog(
    *, source: SkillCatalogSource, search: str = "", limit: int = 24,
) -> dict[str, Any]:
    query = search.strip().casefold()[:200]
    safe_limit = max(1, min(int(limit), 100))
    source_meta = _source(source)
    started_at = time.monotonic()
    async with _http_client() as client:
        async with asyncio.timeout(_CATALOG_OPERATION_TIMEOUT_S):
            revision, entries = await _tree(source, client=client)
        paths = _skill_md_entries(source_meta, entries)
        # The browse view only needs the first page. Loading every SKILL.md
        # made a cold catalog visit fan out into hundreds of GitHub requests.
        # A text search still scans the complete catalog so descriptions and
        # tool requirements remain searchable.
        candidate_paths = paths if query else paths[: safe_limit + 1]
        semaphore = asyncio.Semaphore(12)

        async def load(path: str) -> dict[str, Any] | None:
            source_id = path.removeprefix(f"{source_meta.prefix}/").removesuffix("/SKILL.md")
            try:
                async with semaphore:
                    item = await resolve_skill_catalog_item(
                        source=source,
                        source_id=source_id,
                        _client=client,
                    )
            except (
                TimeoutError,
                httpx.HTTPError,
                UnicodeError,
                SkillParseError,
                ValueError,
                LookupError,
            ):
                return None
            if query and query not in " ".join(
                [item["name"], item["description"], item["source_id"], *item["allowed_tools"]]
            ).casefold():
                return None
            return {
                key: value
                for key, value in item.items()
                if key not in {"skill_md", "body"}
            }

        tasks = {
            path: asyncio.create_task(load(path))
            for path in candidate_paths
        }
        remaining = max(
            0.0,
            _CATALOG_OPERATION_TIMEOUT_S - (time.monotonic() - started_at),
        )
        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=remaining,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # Preserve repository order even though the HTTP requests complete out
        # of order. A slow individual SKILL.md must not discard the successful
        # first-page cards: returning a useful partial page is better than a
        # catalog-wide 502, and `has_more` keeps discovery honest.
        rows_by_path = {
            path: task.result()
            for path, task in tasks.items()
            if task in done and not task.cancelled() and task.exception() is None
        }
        matching_items = [
            row
            for path in candidate_paths
            if (row := rows_by_path.get(path)) is not None
        ]
        if not matching_items and pending:
            raise TimeoutError("Skill catalog did not return any items in time")
        return {
            "source": source,
            "source_label": source_meta.label,
            "revision": revision,
            "items": matching_items[:safe_limit],
            "has_more": (
                len(matching_items) > safe_limit or bool(pending)
                if query
                else len(paths) > safe_limit
            ),
        }


async def read_skill_catalog_file(
    *, source: SkillCatalogSource, source_id: str, path: str,
) -> tuple[bytes, str]:
    clean_id = _clean_source_id(source_id)
    clean_path = path.strip().strip("/")
    if not clean_path or ".." in clean_path.split("/"):
        raise ValueError("invalid Skill file path")
    item = await resolve_skill_catalog_item(source=source, source_id=clean_id)
    known = {entry["path"] for entry in item["files"]}
    if clean_path not in known:
        raise LookupError("Skill file was not found")
    source_meta = _source(source)
    full_path = f"{source_meta.prefix}/{clean_id}/{clean_path}"
    data = await _fetch_bytes(_raw_url(source_meta, full_path))
    content_type = mimetypes.guess_type(clean_path)[0] or "application/octet-stream"
    return data, content_type


async def download_skill_bundle(
    *, source: SkillCatalogSource, source_id: str,
) -> tuple[dict[str, Any], list[tuple[str, str, bytes]]]:
    item = await resolve_skill_catalog_item(source=source, source_id=source_id)
    files_meta = item["files"]
    if len(files_meta) > _MAX_FILES:
        raise ValueError(f"Skill bundle exceeds {_MAX_FILES} files")
    declared_size = sum(int(entry.get("size_bytes") or 0) for entry in files_meta)
    if declared_size > _MAX_BUNDLE_BYTES:
        raise ValueError(f"Skill bundle exceeds {_MAX_BUNDLE_BYTES} bytes")

    semaphore = asyncio.Semaphore(8)

    async def load(entry: dict[str, Any]) -> tuple[str, str, bytes]:
        async with semaphore:
            data, content_type = await read_skill_catalog_file(
                source=source, source_id=source_id, path=entry["path"]
            )
        return entry["path"], content_type, data

    files = list(await asyncio.gather(*(load(entry) for entry in files_meta)))
    if sum(len(data) for _path, _type, data in files) > _MAX_BUNDLE_BYTES:
        raise ValueError(f"Skill bundle exceeds {_MAX_BUNDLE_BYTES} bytes")
    return item, files
