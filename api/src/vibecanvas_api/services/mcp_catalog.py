"""Read-only adapters for public MCP discovery catalogs.

The UI consumes one small, stable shape while each upstream registry keeps its
own schema.  Requests are restricted to the two fixed hosts below; callers
cannot provide a URL, so this service cannot become an SSRF proxy.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from vibecanvas_api.config import config
from vibecanvas_api.services.pinned_http import request_pinned_public_url
from vibecanvas_api.services.public_url import PublicUrlError

CatalogSource = Literal["official", "smithery"]

_OFFICIAL_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"
_SMITHERY_URL = "https://api.smithery.ai/servers"
_CACHE_TTL_S = 300.0
_CATALOG_USER_AGENT = "flowork/1.0 MCP catalog client"
_CATALOG_REQUEST_TIMEOUT_S = 18.0
_REMOTE_AUTH_DISCOVERY_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    payload: dict[str, Any]


_cache: dict[tuple[str, str, int], _CacheEntry] = {}
_detail_cache: dict[tuple[str, str], _CacheEntry] = {}
_cache_lock = asyncio.Lock()


def _trusted_proxy_cidrs() -> tuple[str, ...]:
    if config.sandbox_egress_mode != "proxy":
        return ()
    return config.sandbox_egress_trusted_proxy_cidrs


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _stdio_connection(package: dict[str, Any]) -> tuple[str, list[str]] | None:
    registry_type = _clean_text(package.get("registryType")).lower()
    identifier = _clean_text(package.get("identifier"))
    version = _clean_text(package.get("version"))
    runtime_hint = _clean_text(package.get("runtimeHint"))
    if not identifier:
        return None

    if registry_type == "npm":
        command = runtime_hint or "npx"
        package_ref = f"{identifier}@{version}" if version else identifier
        args = ["-y", package_ref] if command in {"npx", "npm exec"} else [package_ref]
        return command, args
    if registry_type in {"pypi", "python"}:
        command = runtime_hint or "uvx"
        package_ref = f"{identifier}=={version}" if version else identifier
        return command, [package_ref]
    if registry_type in {"oci", "docker"}:
        command = runtime_hint or "docker"
        return command, ["run", "-i", "--rm", identifier]
    return None


def _field_spec(
    raw: dict[str, Any], *, target: str, name: str | None = None,
) -> dict[str, Any] | None:
    field_name = _clean_text(name or raw.get("name"))
    if not field_name:
        return None
    raw_format = _clean_text(raw.get("format") or raw.get("type")).lower()
    input_type = raw_format if raw_format in {"string", "number", "boolean", "filepath"} else "string"
    choices = raw.get("choices") if isinstance(raw.get("choices"), list) else raw.get("enum")
    choices = [str(value) for value in choices] if isinstance(choices, list) else []
    default = raw.get("default")
    return {
        "key": field_name,
        "label": field_name.replace("_", " ").replace("-", " ").title(),
        "description": _clean_text(raw.get("description")),
        "required": bool(raw.get("isRequired")),
        "secret": bool(raw.get("isSecret")),
        "target": target,
        "input_type": input_type,
        "choices": choices,
        "default": default if isinstance(default, (str, int, float, bool)) else None,
        "placeholder": _clean_text(raw.get("placeholder")),
    }


def _remote_fields(remote: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    variables = remote.get("variables") if isinstance(remote.get("variables"), dict) else {}
    for name, raw in variables.items():
        if not isinstance(raw, dict):
            continue
        spec = _field_spec(raw, target=f"url_variable:{name}", name=str(name))
        if spec:
            fields.append(spec)
    headers = remote.get("headers") if isinstance(remote.get("headers"), list) else []
    for raw in headers:
        if not isinstance(raw, dict):
            continue
        header_name = _clean_text(raw.get("name"))
        value_template = _clean_text(raw.get("value"))
        if header_name.casefold() == "authorization" and value_template.casefold().startswith("bearer "):
            placeholder = value_template[7:].strip().strip("{}") or "token"
            spec = _field_spec(raw, target="bearer", name=placeholder)
        else:
            spec = _field_spec(raw, target=f"header:{header_name}")
        if spec:
            fields.append(spec)
    return fields


def _package_fields(package: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    env_vars = package.get("environmentVariables")
    env_vars = env_vars if isinstance(env_vars, list) else []
    for raw in env_vars:
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("name"))
        spec = _field_spec(raw, target=f"env:{name}")
        if spec:
            fields.append(spec)
    return fields


def normalize_official_entry(entry: dict[str, Any]) -> dict[str, Any]:
    server = entry.get("server") if isinstance(entry.get("server"), dict) else {}
    meta_root = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
    meta = meta_root.get("io.modelcontextprotocol.registry/official")
    meta = meta if isinstance(meta, dict) else {}

    source_id = _clean_text(server.get("name"))
    title = _clean_text(server.get("title")) or source_id.rsplit("/", 1)[-1]
    remotes = server.get("remotes") if isinstance(server.get("remotes"), list) else []
    packages = server.get("packages") if isinstance(server.get("packages"), list) else []

    connection: dict[str, Any] | None = None
    config_fields: list[dict[str, Any]] = []
    if remotes and isinstance(remotes[0], dict):
        remote = remotes[0]
        raw_transport = _clean_text(remote.get("type")).lower()
        transport = "sse" if raw_transport == "sse" else "streamable_http"
        endpoint = _clean_text(remote.get("url"))
        if endpoint:
            connection = {
                "transport": transport,
                "endpoint": endpoint,
                "connection_config": {"url": endpoint},
            }
            config_fields = _remote_fields(remote)
    elif packages and isinstance(packages[0], dict):
        stdio = _stdio_connection(packages[0])
        if stdio:
            command, args = stdio
            connection = {
                "transport": "stdio",
                "endpoint": command,
                "connection_config": {"command": command, "args": args},
            }
            config_fields = _package_fields(packages[0])

    repository = server.get("repository") if isinstance(server.get("repository"), dict) else {}
    auth_mode = "configuration" if config_fields else (
        "connection_discovery" if connection and connection["transport"] != "stdio" else "none"
    )
    return {
        "source": "official",
        "source_id": source_id,
        "name": title or source_id,
        "description": _clean_text(server.get("description")),
        "version": _clean_text(server.get("version")) or None,
        "verified": True,
        "usage_count": None,
        "homepage": _clean_text(server.get("websiteUrl")) or _clean_text(repository.get("url")) or None,
        "published_at": _clean_text(meta.get("publishedAt")) or None,
        "connection": connection,
        "config_fields": config_fields,
        "configuration_source": "official_registry",
        "auth_mode": auth_mode,
    }


def normalize_smithery_entry(entry: dict[str, Any]) -> dict[str, Any]:
    source_id = _clean_text(entry.get("qualifiedName"))
    return {
        "source": "smithery",
        "source_id": source_id,
        "name": _clean_text(entry.get("displayName")) or source_id,
        "description": _clean_text(entry.get("description")),
        "version": None,
        "verified": bool(entry.get("verified")),
        "usage_count": int(entry.get("useCount") or 0),
        "homepage": _clean_text(entry.get("homepage")) or None,
        "published_at": _clean_text(entry.get("createdAt")) or None,
        "connection": None,
        "config_fields": [],
        "configuration_source": "smithery_schema",
        "auth_mode": "connection_discovery",
    }


def normalize_smithery_detail(detail: dict[str, Any]) -> dict[str, Any]:
    item = normalize_smithery_entry(detail)
    connections = detail.get("connections") if isinstance(detail.get("connections"), list) else []
    endpoint = _clean_text(detail.get("deploymentUrl"))
    raw_type = ""
    if connections and isinstance(connections[0], dict):
        endpoint = _clean_text(connections[0].get("deploymentUrl")) or endpoint
        raw_type = _clean_text(connections[0].get("type")).lower()
    if endpoint:
        transport = "sse" if raw_type == "sse" else "streamable_http"
        item["connection"] = {
            "transport": transport,
            "endpoint": endpoint,
            "connection_config": {"url": endpoint},
        }
    schema = connections[0].get("configSchema") if connections and isinstance(connections[0], dict) else {}
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") if isinstance(schema.get("required"), list) else [])
    fields: list[dict[str, Any]] = []
    for key, value in properties.items():
        if not isinstance(value, dict):
            continue
        source = value.get("x-from") if isinstance(value.get("x-from"), dict) else {}
        if _clean_text(source.get("header")):
            target = f"header:{_clean_text(source['header'])}"
        else:
            target = f"query:{_clean_text(source.get('query')) or key}"
        spec = _field_spec(
            {
                **value,
                "isRequired": key in required,
                # Smithery's schema does not consistently set writeOnly. A
                # header source or a credential-like field name is still
                # rendered as a password, but the field itself always comes
                # from the authoritative configSchema rather than prose.
                "isSecret": bool(value.get("writeOnly"))
                or "key" in str(key).casefold()
                or "token" in str(key).casefold()
                or "secret" in str(key).casefold(),
            },
            target=target,
            name=str(key),
        )
        if spec:
            spec["label"] = _clean_text(value.get("title")) or spec["label"]
            fields.append(spec)
    item["config_fields"] = fields
    item["auth_mode"] = "configuration" if fields else "connection_discovery"
    return item


async def _fetch_json(url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    # Catalog browsing is an interactive request. A hidden 30 s retry made the
    # detail page look permanently stuck whenever an upstream registry or VPN
    # route was unhealthy. Keep one bounded attempt and let the UI expose a
    # retry action; successful responses are cached below.
    timeout = httpx.Timeout(_CATALOG_REQUEST_TIMEOUT_S, connect=10.0)
    async with asyncio.timeout(_CATALOG_REQUEST_TIMEOUT_S + 0.5):
        response = await request_pinned_public_url(
            "GET",
            url,
            label="MCP catalog URL",
            timeout=timeout,
            headers={"User-Agent": _CATALOG_USER_AGENT},
            params=params,
            allow_redirects=True,
            trusted_proxy_cidrs=_trusted_proxy_cidrs(),
            proxy=config.control_plane_http_proxy or None,
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("MCP catalog returned a non-object response")
    return payload


async def _discover_remote_auth(item: dict[str, Any]) -> dict[str, Any]:
    """Classify a schema-less remote from its protocol response."""
    connection = item.get("connection")
    if not isinstance(connection, dict) or connection.get("transport") == "stdio":
        return item
    if item.get("config_fields"):
        return item
    endpoint = _clean_text(connection.get("endpoint"))
    if not endpoint:
        return item

    timeout = httpx.Timeout(8.0, connect=5.0)
    try:
        async with asyncio.timeout(_REMOTE_AUTH_DISCOVERY_TIMEOUT_S):
            response = await request_pinned_public_url(
                "GET",
                endpoint,
                label="remote MCP endpoint",
                timeout=timeout,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "User-Agent": _CATALOG_USER_AGENT,
                },
                allow_redirects=True,
                trusted_proxy_cidrs=_trusted_proxy_cidrs(),
                proxy=config.control_plane_http_proxy or None,
            )
    except (TimeoutError, httpx.HTTPError, PublicUrlError):
        return item

    challenge = response.headers.get("www-authenticate", "")
    marker = "resource_metadata="
    marker_index = challenge.casefold().find(marker)
    if response.status_code == 401 and marker_index >= 0:
        item["auth_mode"] = "oauth"
        raw_value = challenge[marker_index + len(marker):].lstrip()
        if raw_value.startswith('"'):
            raw_value = raw_value[1:].split('"', 1)[0]
        else:
            raw_value = raw_value.split(",", 1)[0].strip()
        item["auth_metadata_url"] = raw_value or None
    elif response.status_code != 401:
        item["auth_mode"] = "none"
    return item


async def search_catalog(
    *, source: CatalogSource, search: str = "", limit: int = 20,
) -> dict[str, Any]:
    query = search.strip()[:200]
    safe_limit = max(1, min(int(limit), 100))
    cache_key = (source, query.casefold(), safe_limit)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and cached.expires_at > now:
        return cached.payload

    if source == "official":
        request_limit = min(safe_limit + 1, 100)
        params: dict[str, Any] = {"limit": request_limit, "version": "latest"}
        if query:
            params["search"] = query
        raw = await _fetch_json(_OFFICIAL_URL, params=params)
        rows = raw.get("servers") if isinstance(raw.get("servers"), list) else []
        items = [normalize_official_entry(row) for row in rows if isinstance(row, dict)]
        payload = {
            "source": source,
            "ranking": "browse",
            "items": items[:safe_limit],
            "has_more": len(items) > safe_limit,
        }
    else:
        request_limit = min(safe_limit + 1, 100)
        params = {"page": 1, "pageSize": request_limit}
        if query:
            params["q"] = query
        raw = await _fetch_json(_SMITHERY_URL, params=params)
        rows = raw.get("servers") if isinstance(raw.get("servers"), list) else []
        items = [normalize_smithery_entry(row) for row in rows if isinstance(row, dict)]
        payload = {
            "source": source,
            "ranking": "search" if query else "popular",
            "items": items[:safe_limit],
            "has_more": len(items) > safe_limit,
        }

    async with _cache_lock:
        _cache[cache_key] = _CacheEntry(now + _CACHE_TTL_S, payload)
    return payload


async def resolve_catalog_item(*, source: CatalogSource, source_id: str) -> dict[str, Any]:
    clean_id = source_id.strip()[:300]
    if not clean_id:
        raise ValueError("source_id is required")
    cache_key = (source, clean_id)
    now = time.monotonic()
    cached = _detail_cache.get(cache_key)
    if cached and cached.expires_at > now:
        return cached.payload
    if source == "smithery":
        detail = await _fetch_json(f"{_SMITHERY_URL}/{quote(clean_id, safe='/@')}")
        item = await _discover_remote_auth(normalize_smithery_detail(detail))
    else:
        encoded_id = quote(clean_id, safe="")
        detail = await _fetch_json(
            f"{_OFFICIAL_URL}/{encoded_id}/versions/latest",
        )
        item = normalize_official_entry(detail)
        if item.get("source_id") != clean_id:
            raise LookupError("MCP server was not found in the official registry")
        item = await _discover_remote_auth(item)

    async with _cache_lock:
        _detail_cache[cache_key] = _CacheEntry(now + _CACHE_TTL_S, item)
    return item
