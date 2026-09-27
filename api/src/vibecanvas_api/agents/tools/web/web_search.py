"""web_search tool — query the web and return ranked results.

Provider dispatch via config.web_search.provider:

  duckduckgo — Free, no API key required. Uses DuckDuckGo's lightweight HTML
               results endpoint through Requests. Default for the initial
               project. Results: title, URL, snippet.

  tavily     — POST api.tavily.com/search. AI-native, aggregates up to
               20 sources. Requires WEB_SEARCH_API_KEY.

  brave      — GET api.search.brave.com/res/v1/web/search. Independent
               index, $5/1k queries with $5/month free credit.
               Requires WEB_SEARCH_API_KEY.

Provider is read from config.web_search.provider (default: duckduckgo).
Paid providers require WEB_SEARCH_API_KEY; missing key raises ToolError.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.config import config


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _duckduckgo(query: str, max_results: int, timeout: int) -> list[dict]:
    """Free search through DuckDuckGo's HTML endpoint.

    ``duckduckgo-search`` 8.x silently routes text searches through Bing using
    a separate Rust TLS stack. In the sandbox that client intermittently fails
    with ``fatal alert: UnexpectedMessage`` even though normal OpenSSL/Requests
    traffic succeeds. Using the documented HTML surface keeps the network stack
    consistent with the other providers and actually honours our timeout.
    """
    response = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": "Mozilla/5.0 (compatible; floworkSearch/1.0)",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    document = BeautifulSoup(response.text, "html.parser")
    results: list[dict] = []
    for result in document.select(".result"):
        anchor = result.select_one(".result__a")
        if anchor is None:
            continue
        href = str(anchor.get("href") or "")
        # HTML results use a DuckDuckGo redirect URL. Return the actual source
        # URL so agents and the frontend never have to understand that wrapper.
        query_params = parse_qs(urlparse(href).query)
        if query_params.get("uddg"):
            href = unquote(query_params["uddg"][0])
        snippet = result.select_one(".result__snippet")
        results.append(
            {
                "title": anchor.get_text(" ", strip=True),
                "url": href,
                "snippet": (
                    snippet.get_text(" ", strip=True) if snippet is not None else ""
                ),
                "score": None,
            }
        )
        if len(results) >= max_results:
            break
    return results


def _tavily(query: str, max_results: int, timeout: int) -> list[dict]:
    """POST https://api.tavily.com/search"""
    cfg = config.web_search
    if not cfg.api_key:
        raise ToolError("no_api_key", "WEB_SEARCH_API_KEY is not set; configure Tavily credentials")
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": cfg.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_raw_content": False,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("content") or "").strip(),
            "score": r.get("score"),
        }
        for r in data.get("results", [])
    ]


def _brave(query: str, max_results: int, timeout: int) -> list[dict]:
    """GET https://api.search.brave.com/res/v1/web/search"""
    cfg = config.web_search
    if not cfg.api_key:
        raise ToolError("no_api_key", "WEB_SEARCH_API_KEY is not set; configure Brave Search credentials")
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": max_results},
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": cfg.api_key,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("description") or "").strip(),
            "score": None,
        }
        for r in (data.get("web") or {}).get("results", [])
    ]


_PROVIDERS = {
    "duckduckgo": _duckduckgo,
    "tavily": _tavily,
    "brave": _brave,
}


def _search(query: str, max_results: int) -> list[dict]:
    cfg = config.web_search
    provider_fn = _PROVIDERS.get(cfg.provider)
    if provider_fn is None:
        raise ToolError(
            "unknown_provider",
            f"web_search provider '{cfg.provider}' is not supported; "
            f"valid: {', '.join(_PROVIDERS)}",
        )
    try:
        return provider_fn(query, max_results, cfg.timeout)
    except ToolError:
        raise
    except requests.HTTPError as e:
        raise ToolError("http_error", f"{cfg.provider} search failed: {e}")
    except requests.ConnectionError:
        raise ToolError("connection_error", f"could not reach {cfg.provider} search API")
    except requests.Timeout:
        raise ToolError("timeout", f"{cfg.provider} search timed out after {cfg.timeout}s")
    except Exception as e:
        raise ToolError("search_failed", str(e))


async def web_search(query: str, max_results: int = 5) -> tuple:
    """Search the web for titles, URLs and snippets (not full webpage contents).

    Use bash to retrieve a known URL when you need to inspect the source itself.
    max_results defaults to 5. Search failures are errors, not empty results.
    """
    if not query.strip() or max_results < 1:
        result = {"status": "error", "error": {
            "code": "invalid_arguments", "message": "Provide a nonempty query and a positive max_results.",
        }}
    else:
        try:
            results = await asyncio.to_thread(_search, query, max_results)
            result = {"status": "success", "query": query, "results": results}
        except ToolError as exc:
            result = {"status": "error", "error": {"code": str(exc), "message": exc.message}}
    return json.dumps(result, ensure_ascii=False), result
