# -*- coding: utf-8 -*-
"""Plan B B6 — compute the per-run host ALLOWLIST for the sandbox egress proxy.

The sandbox egress proxy (B2/B3/B4) enforces a per-run set of allowed outbound
hosts. In ``proxy`` mode every other host is blocked, so this allowlist must
cover EVERY endpoint a legitimate run reaches:

  * **LLM hosts** — the api_url (and optional proxy) of every saved credential
    a PromptNode / SubAgentNode references, PLUS the platform default base_url
    host when the workflow references a built-in / registered model (which has
    no credential row and resolves to the configured platform endpoint).
  * **Workflow-declared HTTP hosts** — static absolute hosts present in
    HTTPRequestNode URLs. A host containing template interpolation is not
    inferred and must be explicitly allowlisted.
  * **User-declared hosts** — anything the user explicitly added under
    ``__meta__.settings.egress.allowed_hosts`` (the workflow-settings editor).

The CORE union logic (:func:`compute_allow_hosts_from_parts`) is pure / DB-free
so it is exhaustively unit-testable. The sync (:func:`compute_allow_hosts`) and
async (:func:`compute_allow_hosts_async`) wrappers only GATHER the parts (creds
mapping, MCP endpoints, user hosts, built-in base_urls) against their respective
session styles and feed the pure core. ``allow_hosts`` is only consumed in
``proxy`` mode (harmless / ignored in the default host-network mode), so the
runner can always compute + pass it.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import structlog

from vibecanvas_api.config import config
from vibecanvas_api.services.llm_credentials_inject import (
    collect_referenced_credential_names,
    _builtin_model_names,
)

logger = structlog.get_logger(__name__)


# Provider id → the host the engine ACTUALLY dials when a credential's ``api_url``
# is empty/None. The engine's model builder
# (``engine/.../nodes/prompt.py:_build_injected_model`` ~:258-319) falls back to
# ``CANONICAL_PROVIDERS[provider]["default_url"]`` / ``CUSTOM_PROVIDERS[...]`` when
# ``entry["api_url"]`` is falsy. BUT for anthropic / google_genai / gemini the
# engine's ``default_url`` is the EMPTY string (see
# ``engine/.../custom_llms.py`` CANONICAL_PROVIDERS @ ~:406-431 — anthropic="",
# google_genai="") because those SDK clients embed their own default base_url:
#   * OpenAI SDK  → https://api.openai.com/v1   → api.openai.com
#   * Anthropic SDK → https://api.anthropic.com → api.anthropic.com
#   * google.genai (Gemini API) → generativelanguage.googleapis.com
# So we CANNOT derive these three from the engine ``default_url`` strings alone
# (only openai is non-empty). This map records the real default HOST each provider
# resolves to. Keys cover the canonical ids + the legacy/family aliases the engine
# routes (``gemini``/``google`` → GeminiModel). ``azure_openai`` is intentionally
# ABSENT: Azure has no platform-wide default host (each tenant has its own
# ``<resource>.openai.azure.com`` endpoint), so a blank api_url there contributes
# no derivable host. Matched case-insensitively.
_PROVIDER_DEFAULT_HOSTS: dict[str, str] = {
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
    "google_genai": "generativelanguage.googleapis.com",
    "gemini": "generativelanguage.googleapis.com",
    "google": "generativelanguage.googleapis.com",
}


def _provider_default_host(provider: str | None) -> str | None:
    """The default egress host for a provider id when its credential carries no
    explicit ``api_url`` (mirrors the engine's empty-api_url fallback). ``None``
    for an unknown provider or Azure (no platform-wide default host)."""
    if not provider or not isinstance(provider, str):
        return None
    return _PROVIDER_DEFAULT_HOSTS.get(provider.strip().lower())


def _host_of(url_or_host: str | None) -> str | None:
    """Extract the bare host (lowercased, no scheme / path / port) from a URL
    string OR a bare host[:port] string.

    Returns ``None`` for empty / None input. A value with no ``://`` scheme is
    treated as a host[:port] (the common user-declared shape, e.g.
    ``"api.example.com"`` or ``"h.test:80"``)."""
    if not url_or_host or not isinstance(url_or_host, str):
        return None
    s = url_or_host.strip()
    if not s:
        return None
    # urlsplit only populates ``.hostname`` when a ``//`` authority is present.
    # Bare hosts (no scheme) land entirely in ``.path`` → prepend a dummy scheme
    # so the authority parser kicks in and strips any ``:port`` for us.
    if "://" not in s:
        s = "//" + s
    parts = urlsplit(s)
    host = parts.hostname  # already lowercased + port-stripped by urlsplit
    if not host:
        return None
    return host.lower()


def _platform_base_url() -> str | None:
    """The configured platform default LLM endpoint base_url.

    Built-in / registered models (no per-tenant credential row) resolve to the
    platform's own gateway. We use the agent chat config's ``base_url`` as the
    canonical platform endpoint used by registered models. May be empty when
    unconfigured (dev) → caller
    treats ``None`` as "nothing to add"."""
    return getattr(config.agent, "base_url", None) or None


def compute_allow_hosts_from_parts(
    creds_mapping: dict,
    mcp_endpoints: list[str],
    user_hosts: list[str],
    builtin_base_urls: list[str],
) -> set[str]:
    """Pure union of every host the run is allowed to reach. DB-free.

    * ``creds_mapping``: ``{name: {api_url, proxy, ...}}`` (the resolved
      ``llm_credentials`` extra). We pull ``_host_of`` of each entry's
      ``api_url`` and ``proxy``.
    * ``mcp_endpoints``: optional integration endpoint URLs supplied by a
      caller. Workflow settings no longer carry per-workflow MCP selections.
    * ``user_hosts``: user-declared host / URL strings.
    * ``builtin_base_urls``: platform default base_url(s) to include when the
      workflow references a built-in / registered model.

    Returns the set of bare lowercased hosts; ``None`` (unparseable / empty)
    entries are dropped."""
    hosts: set[str] = set()

    for entry in (creds_mapping or {}).values():
        if not isinstance(entry, dict):
            continue
        for key in ("api_url", "proxy"):
            h = _host_of(entry.get(key))
            if h:
                hosts.add(h)
        # When the credential carries NO explicit api_url, the engine dials the
        # provider's hardcoded default host (e.g. provider="openai" + api_url=None
        # → api.openai.com). Add it so a blank-api_url saved credential isn't
        # blocked in prod proxy mode. An entry WITH api_url uses that url (above)
        # and this is redundant — but harmless, since both point at the provider.
        if not entry.get("api_url"):
            dh = _provider_default_host(entry.get("provider"))
            if dh:
                hosts.add(dh)

    for endpoint in (mcp_endpoints or []):
        h = _host_of(endpoint)
        if h:
            hosts.add(h)

    for uh in (user_hosts or []):
        h = _host_of(uh)
        if h:
            hosts.add(h)

    for base in (builtin_base_urls or []):
        h = _host_of(base)
        if h:
            hosts.add(h)

    return hosts


# ---------------------------------------------------------------------------
# Part gatherers (shared shapes; sync + async wrappers feed the pure core)
# ---------------------------------------------------------------------------


def _settings(workflow_dict: dict) -> dict:
    """The ``__meta__.settings`` sub-dict (or ``{}`` when absent)."""
    meta = (workflow_dict or {}).get("__meta__") or {}
    return meta.get("settings") or {}


def _user_declared_hosts(workflow_dict: dict) -> list[str]:
    """User-declared hosts from ``settings.egress.allowed_hosts`` (list[str];
    ``[]`` when absent)."""
    egress = _settings(workflow_dict).get("egress") or {}
    hosts = egress.get("allowed_hosts") or []
    if not isinstance(hosts, list):
        return []
    return [h for h in hosts if isinstance(h, str) and h.strip()]


def _workflow_declared_http_hosts(workflow_dict: dict) -> list[str]:
    """Return static hosts explicitly authored in HTTPRequestNode URLs.

    The URL is already part of the workflow's executable contract, so granting
    its literal host does not broaden authority beyond what the node declares.
    Dynamic/interpolated authorities are intentionally not inferred: an Agent
    or user must list those destinations in Workflow Settings after reviewing
    the possible values.
    """
    hosts: list[str] = []
    for node_id, node in (workflow_dict or {}).items():
        if node_id == "__meta__" or not isinstance(node, dict):
            continue
        if node.get("node_type") != "HTTPRequestNode":
            continue
        url = (node.get("node_config") or {}).get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        parts = urlsplit(url.strip())
        authority = parts.netloc
        if parts.scheme not in {"http", "https"} or not authority:
            continue
        if "{{" in authority or "}}" in authority:
            continue
        if parts.hostname:
            hosts.append(parts.hostname.lower())
    return hosts


def _builtin_base_urls(workflow_dict: dict, creds_mapping: dict) -> list[str]:
    """The platform base_url(s) to include WHEN the workflow references at least
    one built-in / registered model (i.e. a referenced model_name that is NOT a
    saved-credential name resolved into ``creds_mapping``).

    First include the configured platform default ``base_url`` host. We do not
    enumerate every registered model's hardcoded base_url — they all route the
    same gateway and a precise per-model map is out of scope here."""
    builtins = _builtin_model_names()
    referenced_saved = collect_referenced_credential_names(workflow_dict)
    has_builtin_ref = False
    for node_id, node in (workflow_dict or {}).items():
        if node_id == "__meta__" or not isinstance(node, dict):
            continue
        if node.get("node_type") not in ("PromptNode", "SubAgentNode"):
            continue
        name = (node.get("node_config") or {}).get("model_name")
        if not isinstance(name, str) or not name:
            continue
        # A reference is "built-in" when it is a registered/custom model name OR
        # it is not one of the saved names we actually resolved.
        if name in builtins or name not in referenced_saved:
            has_builtin_ref = True
            break
    if not has_builtin_ref:
        return []
    bases: list[str] = []
    base = _platform_base_url()
    if base:
        bases.append(base)
    # A referenced built-in/registered model resolves to a provider client whose
    # base_url we do NOT individually enumerate here (each registered model class
    # embeds its own; a precise per-model map is out of scope). To avoid blocking
    # the common case, also include every KNOWN provider default host (openai /
    # anthropic / gemini). This is INTENTIONALLY broad — it permits the platform
    # provider hosts whenever ANY built-in is referenced, rather than the exact
    # host of the specific built-in. LIMITATION: a registered model pointing at a
    # provider NOT in ``_PROVIDER_DEFAULT_HOSTS`` (or a private gateway other than
    # ``config.agent.base_url``) would still need a user-declared allowed_host.
    bases.extend(_PROVIDER_DEFAULT_HOSTS.values())
    return bases


def compute_allow_hosts(
    workflow_dict: dict,
    *,
    user_id: str,
    session=None,
    creds_mapping: dict | None = None,
) -> set[str]:
    """SYNC wrapper — gather parts via the sync short-session facade and feed the
    pure core. Used by ``run_workflow_sandboxed_sync`` (a sync background worker / to_thread
    context with the tenant carried in ``current_sync_tenant_id``).

    ``creds_mapping`` should be the already-minted broker mapping staged for the
    run. Re-resolving credentials here would mint a second unused capability and
    risks diverging from the exact descriptor the sandbox receives.

    Fail-soft: any error gathering a part logs + degrades to the parts already
    gathered (it never silently opens everything — at minimum the derived LLM
    hosts survive)."""
    del user_id, session
    creds_mapping = creds_mapping or {}

    user_hosts = [
        *_workflow_declared_http_hosts(workflow_dict),
        *_user_declared_hosts(workflow_dict),
    ]
    builtin_base_urls = _builtin_base_urls(workflow_dict, creds_mapping)

    return compute_allow_hosts_from_parts(
        creds_mapping, [], user_hosts, builtin_base_urls)


async def compute_allow_hosts_async(
    workflow_dict: dict,
    *,
    session,
    user_id: str,
    creds_mapping: dict | None = None,
) -> set[str]:
    """ASYNC wrapper — gather parts on the running event loop and feed the pure
    core. Used by the executions route (``_produce_execution_sandbox``).

    ``creds_mapping`` is the PRE-RESOLVED broker mapping produced by the route.
    It is never rebuilt here because capabilities are execution-scoped.

    Fail-soft, same as the sync wrapper."""
    del session, user_id
    cm = creds_mapping or {}

    user_hosts = [
        *_workflow_declared_http_hosts(workflow_dict),
        *_user_declared_hosts(workflow_dict),
    ]
    builtin_base_urls = _builtin_base_urls(workflow_dict, cm)

    return compute_allow_hosts_from_parts(
        cm, [], user_hosts, builtin_base_urls)
