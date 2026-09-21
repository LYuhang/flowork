"""Centralized application configuration — the single second layer.

Three-layer config flow:
    config.yaml  →  AppConfig (this file)  →  usage sites

config.yaml holds raw parameters only. AppConfig reads them once at
startup, computes derived values (access tokens, resolved paths, etc.),
and exposes typed properties. Usage sites import ``config`` and access
scoped attributes — never touching yaml or raw dicts.

Usage:
    from app_config import config

    root = config.storage.root          # Path object
    model = config.agent.model          # str
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import yaml

from vibecanvas_api.services.agent_runtime.registry import AVAILABLE_RUNTIME_TYPES

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _as_bool(raw_val, env_val, *, default: bool) -> bool:
    """Coerce a yaml value or env string to bool; env wins, then yaml, then default."""
    for v in (env_val, raw_val):
        if v is None:
            continue
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    return default


def _unix_permission_mode(value: Any, *, default: int, setting: str) -> int:
    """Parse a private owner/group Unix mode without allowing world access."""
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().lower().removeprefix("0o")
    try:
        mode = int(text, 8)
    except ValueError as exc:
        raise ValueError(f"{setting} must be an octal Unix mode") from exc
    if not 0 <= mode <= 0o777 or mode & 0o007:
        raise ValueError(f"{setting} must not grant access to other users")
    return mode


def _csv_choice_set(
    value: Any,
    *,
    default: tuple[str, ...],
    allowed: frozenset[str],
    setting: str,
) -> tuple[str, ...]:
    raw_values = default if value is None else tuple(str(value).split(","))
    normalized = tuple(
        dict.fromkeys(item.strip().lower() for item in raw_values if item.strip())
    )
    if not normalized or any(item not in allowed for item in normalized):
        raise ValueError(
            f"{setting} must contain one or more of: {', '.join(sorted(allowed))}"
        )
    return normalized


def _csv_values(value: Any) -> tuple[str, ...]:
    """Return a stable, de-duplicated tuple from a comma-separated setting."""
    if value is None:
        return ()
    return tuple(
        dict.fromkeys(item.strip() for item in str(value).split(",") if item.strip())
    )


def _private_egress_targets(value: Any) -> tuple[tuple[str, int], ...]:
    """Parse trusted private sandbox destinations as exact host/port pairs."""
    targets: list[tuple[str, int]] = []
    for item in _csv_values(value):
        try:
            parts = urlsplit(f"//{item}")
            host = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise ValueError(
                "SANDBOX_EGRESS_PRIVATE_TARGETS must contain host:port values"
            ) from exc
        if (
            not host
            or port is None
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "SANDBOX_EGRESS_PRIVATE_TARGETS must contain host:port values"
            )
        target = (host.lower(), port)
        if target not in targets:
            targets.append(target)
    return tuple(targets)


def _trusted_proxy_cidrs(value: Any) -> tuple[str, ...]:
    """Validate operator-owned synthetic DNS/proxy networks."""
    networks: list[str] = []
    for item in _csv_values(value):
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise ValueError(
                "SANDBOX_EGRESS_TRUSTED_PROXY_CIDRS must contain CIDR networks"
            ) from exc
        # A trusted synthetic range must remain narrow. Broad routes could turn
        # the public policy into access to an entire private address family.
        minimum_prefix = 8 if network.version == 4 else 32
        if network.prefixlen < minimum_prefix:
            raise ValueError(
                "SANDBOX_EGRESS_TRUSTED_PROXY_CIDRS entries are too broad"
            )
        rendered = str(network)
        if rendered not in networks:
            networks.append(rendered)
    return tuple(networks)


def _control_plane_http_proxy(value: Any) -> str:
    """Validate the operator-owned proxy used by host-side HTTP clients."""
    rendered = str(value or "").strip()
    if not rendered:
        return ""
    try:
        parts = urlsplit(rendered)
        port = parts.port
    except ValueError as exc:
        raise ValueError(
            "FLOWORK_CONTROL_PLANE_HTTP_PROXY must be an HTTP(S) proxy URL"
        ) from exc
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or port is None
    ):
        raise ValueError(
            "FLOWORK_CONTROL_PLANE_HTTP_PROXY must be an HTTP(S) proxy URL "
            "with an explicit port"
        )
    return rendered.rstrip("/")


def _parse_mapping_env(name: str) -> Dict[str, Any]:
    raw = os.environ.get(name)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError) as exc:
            print(f"[app_config] failed to parse {name}: {exc}")
            return {}
    if not isinstance(value, dict):
        print(f"[app_config] ignored {name}: expected a dict/object")
        return {}
    return value


def _openfga_bootstrap_config() -> Dict[str, Any]:
    """Read non-secret dev store/model IDs produced by the bootstrap job."""
    path = os.environ.get("OPENFGA_BOOTSTRAP_CONFIG_FILE")
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _default_agent_api_env() -> Dict[str, Any]:
    """Server-injected default model credential for dev/demo environments.

    Preferred env var: ``VIBECANVAS_DEFAULT_AGENT_API``.
    Short alias: ``DEFAULT_API``.

    Supported keys intentionally mirror the persisted credential model while
    accepting common aliases:
      provider, model/model_name, model_context_tokens/model_context_length,
      base_url/api_url, proxy, api_key.
    """
    return _parse_mapping_env("VIBECANVAS_DEFAULT_AGENT_API") or _parse_mapping_env(
        "DEFAULT_API"
    )


def _codex_managed_apis_env(default_model: str) -> list[Dict[str, Any]]:
    """Parse trusted operator-managed Codex API profiles.

    This configuration is intentionally process-only: API keys and full
    destinations are never copied into user preferences or public responses.
    """
    raw = os.environ.get("CODEX_MANAGED_APIS_JSON", "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("CODEX_MANAGED_APIS_JSON must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("CODEX_MANAGED_APIS_JSON must be a JSON array")
    fallback_model = default_model.partition(":")[2] or default_model
    profiles: list[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(
                f"CODEX_MANAGED_APIS_JSON[{index}] must be an object"
            )
        name = str(item.get("name") or "").strip()
        base_url = str(item.get("base_url") or item.get("api_url") or "").strip()
        api_key = str(item.get("api_key") or "").strip()
        explicit_id = str(item.get("id") or "").strip()
        profile_id = explicit_id or uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vibecanvas:codex-managed-api:{name}",
        ).hex[:16]
        if (
            not name
            or len(name) > 100
            or not profile_id
            or len(profile_id) > 100
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in profile_id)
        ):
            raise ValueError(
                f"CODEX_MANAGED_APIS_JSON[{index}] has an invalid id or name"
            )
        if profile_id in seen_ids:
            raise ValueError(f"duplicate managed Codex API id: {profile_id}")
        parts = urlsplit(base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                f"CODEX_MANAGED_APIS_JSON[{index}] has an invalid base_url"
            )
        if not api_key:
            raise ValueError(
                f"CODEX_MANAGED_APIS_JSON[{index}] requires api_key"
            )
        configured_models = item.get("models")
        if configured_models is None:
            configured_models = [item.get("model") or fallback_model]
        elif isinstance(configured_models, str):
            configured_models = [configured_models]
        if not isinstance(configured_models, list):
            raise ValueError(
                f"CODEX_MANAGED_APIS_JSON[{index}].models must be an array"
            )
        models = tuple(
            dict.fromkeys(
                str(model).strip()
                for model in configured_models
                if str(model).strip()
            )
        )
        if not models:
            raise ValueError(
                f"CODEX_MANAGED_APIS_JSON[{index}] requires at least one model"
            )
        profiles.append({
            "id": profile_id,
            "name": name,
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "models": models,
        })
        seen_ids.add(profile_id)
    return profiles


def _canonical_provider(raw: Any) -> str:
    provider = str(raw or "").strip()
    if not provider:
        return ""
    key = provider.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "openai": "openai",
        "azure": "azure_openai",
        "azure_openai": "azure_openai",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "google": "google_genai",
        "google_genai": "google_genai",
        "google_gemini": "google_genai",
        "gemini": "google_genai",
    }
    return aliases.get(key, key)


# ---------------------------------------------------------------------------
# Scope classes
# ---------------------------------------------------------------------------


class CompactionV2Config:
    """Form-ladder compaction knobs (spec 2026-06-22 §5), centralized here.

    Read from the ``agent.compaction.v2`` sub-block so the new form-ladder system
    is configured separately from the live s2a/s2b flat knobs (which stay on
    ``AgentConfig`` for back-compat). Every value has a default == the §5 table.
    """

    _DEFAULT_TIERS = [
        {"name": "none", "max_tokens": 2000, "full_rounds": None},  # never age-decays
        {"name": "S", "max_tokens": 10000, "full_rounds": 8},
        {"name": "M", "max_tokens": 100000, "full_rounds": 4},
        {"name": "L", "max_tokens": 200000, "full_rounds": 2},
        {"name": "XL", "max_tokens": None, "full_rounds": 1},  # 500k+
    ]
    _DEFAULT_STALE = ["get_workflow", "read_file", "show_on_canvas"]
    _DEFAULT_FILE_CONTEXT_TIERS = [
        {"max_tokens": 2000, "full_rounds": None},
        {"max_tokens": 16000, "full_rounds": 16},
        {"max_tokens": 32000, "full_rounds": 8},
        {"max_tokens": 64000, "full_rounds": 1},
        {"max_tokens": None, "full_rounds": 0},
    ]

    def __init__(self, raw: Dict[str, Any]):
        # form-ladder thresholds
        self.inline_chars: int = int(raw.get("inline_chars", 16000))
        # A non-viewer tool whose raw
        # output exceeds ``inline_chars`` truncates to a head+tail PREVIEW + a notice + a VFS
        # file ref (full body offloaded to ``offload_dir``); the agent re-reads the full via
        # read_file (the sole content viewer). ``offload_preview_chars`` is split head/tail.
        self.offload_preview_chars: int = int(raw.get("offload_preview_chars", 800))
        self.offload_dir: str = str(raw.get("offload_dir", "/memory/outputs"))
        self.protect_recent_rounds: int = int(raw.get("protect_recent_rounds", 3))
        # Interactive artifacts use the same compaction/offload machinery as tool
        # outputs, but keep separate override points so product can tune the
        # widget payload policy without changing every other tool output.
        self.interactive_artifact_inline_chars: int = int(
            raw.get("interactive_artifact_inline_chars", self.inline_chars)
        )
        self.interactive_artifact_offload_preview_chars: int = int(
            raw.get(
                "interactive_artifact_offload_preview_chars", self.offload_preview_chars
            )
        )
        self.interactive_artifact_offload_dir: str = str(
            raw.get(
                "interactive_artifact_offload_dir",
                f"{self.offload_dir.rstrip('/')}/interactive",
            )
        )
        self.interactive_artifact_protect_recent_rounds: int = int(
            raw.get(
                "interactive_artifact_protect_recent_rounds", self.protect_recent_rounds
            )
        )
        self.interactive_artifact_resource_ttl_s: int = int(
            raw.get("interactive_artifact_resource_ttl_s", 3600)
        )
        self.interactive_artifact_state_max_chars: int = int(
            raw.get("interactive_artifact_state_max_chars", 1_000_000)
        )
        self.interactive_artifact_draft_debounce_ms: int = int(
            raw.get("interactive_artifact_draft_debounce_ms", 600)
        )
        self.interactive_artifact_result_dir: str = str(
            raw.get("interactive_artifact_result_dir", "/data/interactive")
        ).rstrip("/")
        self.size_tiers: list = raw.get("size_tiers") or self._DEFAULT_TIERS
        # pressure thresholds (fraction of model context window)
        self.pressure_abstract: float = float(raw.get("pressure_abstract", 0.50))
        self.pressure_summary: float = float(raw.get("pressure_summary", 0.80))
        self.hysteresis_target: float = float(raw.get("hysteresis_target", 0.50))
        # batched re-segment / KV-cache economics (§4.0)
        self.resegment_every_rounds: int = int(raw.get("resegment_every_rounds", 8))
        self.clear_at_least: int = int(raw.get("clear_at_least", 20000))
        self.pin_first_exchange: bool = bool(raw.get("pin_first_exchange", True))
        # error protection (§4.6): None = status:error never degrades (A/B1)
        self.error_protect_rounds = raw.get("error_protect_rounds", None)
        # stale re-read supersession (§4.5)
        self.stale_on_reread_tools: list = raw.get("stale_on_reread_tools") or list(
            self._DEFAULT_STALE
        )
        # content_compress (selective per-output LLM gist, §4.1)
        self.compress_single_tokens: int = int(raw.get("compress_single_tokens", 30000))
        self.compress_pressure: float = float(raw.get("compress_pressure", 0.80))
        # multimodal lane (§6.1)
        self.aux_full_rounds: int = int(raw.get("aux_full_rounds", 2))
        # LLM
        self.summarizer_version: str = str(raw.get("summarizer_version", "v1"))
        # model context window (tokens) — pressure thresholds are a FRACTION of this
        self.window_tokens: int = int(raw.get("window_tokens", 200000))
        # Dynamic file context policy for read_file/write_file/edit_file outputs.
        # full_rounds=None means keep the full output until global pressure
        # compaction acts. full_rounds=0 means abbreviate immediately.
        self.file_context_tiers: list = raw.get("file_context_tiers") or list(
            self._DEFAULT_FILE_CONTEXT_TIERS
        )
        self.file_context_head_tokens: int = int(
            raw.get("file_context_head_tokens", 2000)
        )
        self.file_context_tail_tokens: int = int(
            raw.get("file_context_tail_tokens", 2000)
        )
        self.file_input_head_tokens: int = int(raw.get("file_input_head_tokens", 512))
        self.file_input_tail_tokens: int = int(raw.get("file_input_tail_tokens", 512))
        # Compatibility flag for selecting the newer compaction path.
        self.v2_enabled: bool = bool(raw.get("v2_enabled", False))
        # Rollout is independent from the emergency compatibility flag. Shadow
        # always builds observability without changing model input; canary uses
        # a stable tenant/workspace bucket; full enables active compaction.
        self.rollout_mode: str = str(
            raw.get("rollout_mode", "full" if self.v2_enabled else "shadow")
        ).lower()
        if self.rollout_mode not in {"off", "shadow", "canary", "full"}:
            raise ValueError(
                "compaction v2 rollout_mode must be off/shadow/canary/full"
            )
        self.canary_percent: int = max(0, min(100, int(raw.get("canary_percent", 0))))
        self.canary_tenants: list[str] = [
            str(value) for value in raw.get("canary_tenants", [])
        ]

    def to_runtime_dict(self) -> Dict[str, Any]:
        """Return the complete serializable policy consumed in the sandbox."""
        return {
            key: value for key, value in vars(self).items() if not key.startswith("_")
        }

    def tier_of(self, raw_tokens: int) -> dict:
        """The size tier (dict) for a raw-token count. First tier whose
        ``max_tokens`` is None (unbounded) or ``>= raw_tokens``."""
        for tier in self.size_tiers:
            mx = tier.get("max_tokens")
            if mx is None or raw_tokens <= mx:
                return tier
        return self.size_tiers[-1]


class AgentConfig:
    """Shared model and context-window configuration.

    `api_key` should NOT be committed to config.yaml. Leave it blank
    and supply ``AGENT_API_KEY`` via the environment.

    Runtime adapters receive only the serializable subset they support.
    """

    def __init__(self, raw: Dict[str, Any]):
        default_api = _default_agent_api_env()
        default_provider = _canonical_provider(default_api.get("provider"))
        default_model_name = (
            default_api.get("model") or default_api.get("model_name") or ""
        )
        if (
            default_provider
            and default_model_name
            and ":" not in str(default_model_name)
        ):
            default_model = f"{default_provider}:{default_model_name}"
        else:
            default_model = str(default_model_name or "")

        self.model: str = (
            default_model or raw.get("model") or ""
        )
        self.base_url: str = (
            default_api.get("base_url")
            or default_api.get("api_url")
            or raw.get("base_url")
            or ""
        )
        self.proxy: str = default_api.get("proxy") or raw.get("proxy") or ""
        self.api_key: str = (
            default_api.get("api_key")
            or raw.get("api_key")
            or os.environ.get("AGENT_API_KEY", "")
        )
        if _as_bool(
            None,
            os.environ.get("VIBECANVAS_DISABLE_PLATFORM_DEFAULT_API"),
            default=False,
        ):
            # Explicit fail-closed deployment/test mode. Ignore every legacy
            # platform source, including config.yaml, so only user-owned or
            # explicitly managed credentials can be selected.
            self.model = ""
            self.base_url = ""
            self.proxy = ""
            self.api_key = ""
        self.temperature: Optional[float] = raw.get("temperature")
        self.timeout: Optional[int] = raw.get("timeout")
        self.max_retries: Optional[int] = raw.get("max_retries")
        self.extra_body: Dict[str, Any] = raw.get("extra_body", {})
        self.use_responses_api: Optional[bool] = raw.get("use_responses_api")
        self.reasoning: Optional[Dict[str, Any]] = raw.get("reasoning")
        self.output_version: Optional[str] = raw.get("output_version")
        self.model_context_tokens: Optional[int] = None
        raw_context_tokens = (
            default_api.get("model_context_tokens")
            or default_api.get("model_context_length")
            or default_api.get("context_window_tokens")
            or raw.get("model_context_tokens")
            or raw.get("model_context_length")
            or raw.get("context_window_tokens")
        )
        if raw_context_tokens is not None:
            try:
                tokens = int(raw_context_tokens)
                if tokens > 0:
                    self.model_context_tokens = tokens
            except (TypeError, ValueError):
                print(
                    "[app_config] ignored default agent model context length: expected positive integer"
                )

        # Context-lifecycle middleware tuning (LifecyclePolicyEdit). Sourced
        # from an optional ``agent.context`` block; defaults match the edit's
        # own defaults so the middleware behaves identically if omitted.
        context = raw.get("context") or {}
        context_trigger_default = (
            int(self.model_context_tokens * 0.50)
            if self.model_context_tokens
            else 80_000
        )
        context_clear_default = (
            int(self.model_context_tokens * 0.10)
            if self.model_context_tokens
            else 20_000
        )
        self.context_trigger: int = int(context.get("trigger", context_trigger_default))
        self.context_clear_at_least: int = int(
            context.get("clear_at_least", context_clear_default)
        )
        self.context_max_node_specs: int = int(context.get("max_node_specs", 5))

        # Reserved config slot for the LLM-compaction stages (S2a/S2b). A
        # separate (typically cheaper) utility model can be wired here later with
        # without changing call sites. When blank, the
        # compaction code reads `compaction_model` → falls back to the agent
        # model. Per-message token counting always uses the active
        # agent model, not this slot.
        compaction = raw.get("compaction") or {}
        self.compaction_model: str = compaction.get("model") or ""
        # A tool output becomes eligible for S2a when
        # ``meta.tokens.raw`` (or recorded ``output.full_tokens``) exceeds this
        # many tokens is a candidate for the large-output FORM. Default 8000
        # This threshold is shared by the default head/tail tier and the
        # S2a LLM-gist upgrade.
        self.s2a_oversize_tokens: int = int(compaction.get("s2a_oversize_tokens", 8000))
        # The default representation for a fresh large
        # output is head+tail+notice (deterministic, no LLM) — the middleware reads
        # the FULL body from VFS by path and shows head(N)+notice+tail(M). S2a (the
        # LLM-gist upgrade) is OPT-IN and OFF BY DEFAULT.
        self.s2a_enabled: bool = bool(compaction.get("s2a_enabled", False))
        self.headtail_head_tokens: int = int(
            compaction.get("headtail_head_tokens", 1500)
        )
        self.headtail_tail_tokens: int = int(
            compaction.get("headtail_tail_tokens", 500)
        )
        # S2b is the whole-prefix LLM summary used as a long-session safety net.
        # ON BY DEFAULT (unlike opt-in S2a): when the running estimate crosses
        # ``summary_trigger_tokens`` the oldest prefix is collapsed into one cached
        # summary, in ONE shot, until the projected estimate drops below
        # ``summary_target_tokens`` (hysteresis — no per-turn thrash). The pinned
        # head (system + first human) + a recent live tail are never summarized.
        self.s2b_enabled: bool = bool(compaction.get("s2b_enabled", True))
        summary_trigger_default = (
            int(self.model_context_tokens * 0.80)
            if self.model_context_tokens
            else 120_000
        )
        summary_target_default = (
            int(self.model_context_tokens * 0.45)
            if self.model_context_tokens
            else 60_000
        )
        self.summary_trigger_tokens: int = int(
            compaction.get("summary_trigger_tokens", summary_trigger_default)
        )
        self.summary_target_tokens: int = int(
            compaction.get("summary_target_tokens", summary_target_default)
        )
        self.summary_pinned_head: int = int(compaction.get("summary_pinned_head", 2))
        self.summary_live_tail: int = int(compaction.get("summary_live_tail", 4))

        # Form-ladder compaction v2 keeps all knobs in the
        # ``agent.compaction.v2`` sub-block. Separate from the live s2a/s2b knobs
        # above and is gated by ``compaction_v2.v2_enabled`` (default off).
        compaction_v2_raw = dict(compaction.get("v2") or {})
        if self.model_context_tokens and "window_tokens" not in compaction_v2_raw:
            compaction_v2_raw["window_tokens"] = self.model_context_tokens
        self.compaction_v2 = CompactionV2Config(compaction_v2_raw)
        resilience = dict(raw.get("resilience") or {})
        self.max_model_calls: int = int(resilience.get("max_model_calls", 32))
        self.max_tool_calls: int = int(resilience.get("max_tool_calls", 64))
        self.turn_wall_clock_s: float = float(resilience.get("turn_wall_clock_s", 900))
        self.model_retries: int = int(resilience.get("model_retries", 1))
        self.read_tool_retries: int = int(resilience.get("read_tool_retries", 1))

    def resolve_compaction_model(self) -> str:
        """The model the LLM-compaction stages (S2a/S2b) should use.

        Implements `config.agent.compaction.model or <the agent's model>` (spec
        §8 q4). The slot is RESERVED — not wired into any compaction call yet —
        so a cheaper utility model can be configured later with no call-site
        change. Token counting (§4.6) does NOT go through here; it uses
        ``self.model`` directly."""
        return self.compaction_model or self.model

    def to_init_kwargs(self) -> Dict[str, Any]:
        """Build provider-neutral model invocation options."""
        kwargs: Dict[str, Any] = {}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.temperature is not None:
            kwargs["temperature"] = float(self.temperature)
        if self.timeout is not None:
            kwargs["timeout"] = int(self.timeout)
        if self.max_retries is not None:
            kwargs["max_retries"] = int(self.max_retries)
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        if self.use_responses_api is not None:
            kwargs["use_responses_api"] = bool(self.use_responses_api)
        if self.reasoning:
            kwargs["reasoning"] = self.reasoning
        if self.output_version:
            kwargs["output_version"] = self.output_version
        return kwargs

    def to_agent_cfg(self) -> Dict[str, Any]:
        """Build the serialisable runtime agent config.

        This includes app-owned fields such as ``model_context_tokens`` and
        ``proxy`` that should ride through middleware/config resolution, while
        ``to_init_kwargs`` stays restricted to model-constructor arguments.
        """
        cfg = self.to_init_kwargs() | {"model": self.model}
        if self.proxy:
            cfg["proxy"] = self.proxy
        if self.model_context_tokens:
            cfg["model_context_tokens"] = self.model_context_tokens
        cfg.update(
            {
                "context_trigger": self.context_trigger,
                "context_clear_at_least": self.context_clear_at_least,
                "context_max_node_specs": self.context_max_node_specs,
                "compaction_model": self.compaction_model,
                "s2a_enabled": self.s2a_enabled,
                "s2a_oversize_tokens": self.s2a_oversize_tokens,
                "headtail_head_tokens": self.headtail_head_tokens,
                "headtail_tail_tokens": self.headtail_tail_tokens,
                "s2b_enabled": self.s2b_enabled,
                "summary_trigger_tokens": self.summary_trigger_tokens,
                "summary_target_tokens": self.summary_target_tokens,
                "summary_pinned_head": self.summary_pinned_head,
                "summary_live_tail": self.summary_live_tail,
                "compaction_v2": self.compaction_v2.to_runtime_dict(),
                "max_model_calls": self.max_model_calls,
                "max_tool_calls": self.max_tool_calls,
                "turn_wall_clock_s": self.turn_wall_clock_s,
                "model_retries": self.model_retries,
                "read_tool_retries": self.read_tool_retries,
            }
        )
        return cfg


class StorageConfig:
    """Resolved storage paths — directories are created on init.

    Attributes:
        root: Application-owned local storage root.
        users / appendix: subdirectories of ``root``.
    """

    def __init__(
        self,
        root_override: Optional[str] = None,
        vfs_upload_max_bytes: Optional[int] = None,
    ):
        self.root: Path = Path(
            root_override
            or os.environ.get(
                "VIBECANVAS_STORAGE_ROOT",
                Path(__file__).parent / "local_data",
            )
        ).resolve()
        self.users: Path = self.root / "users"
        self.appendix: Path = self.root / "appendix"

        # Durable VFS upload size cap. A user uploads datasets and media to
        # `/mount` or Chat `/data`, so this is generous (≈50MB, mirroring KB's
        # MAX_FILE_SIZE_BYTES) — NOT the 256KB read-side VFS_HTTP_MAX_BYTES
        # (an agent-context read bound, a different concern). Env > yaml > default.
        self.vfs_upload_max_bytes: int = int(
            os.environ.get("VIBECANVAS_VFS_UPLOAD_MAX_BYTES")
            or vfs_upload_max_bytes
            or 50 * 1024 * 1024
        )

        # Optional self-hosted, human-readable bridge for the user-level
        # `/mount` VFS. MOUNT_PATH is a ROOT only; each identity receives a
        # stable child directory and can never choose a host path itself.
        raw_mount_path = os.environ.get("MOUNT_PATH", "").strip()
        self.mount_path: Path | None = None
        if raw_mount_path:
            candidate = Path(raw_mount_path)
            if not candidate.is_absolute():
                raise ValueError("MOUNT_PATH must be an absolute path")
            if candidate.exists() and candidate.is_symlink():
                raise ValueError("MOUNT_PATH must not be a symbolic link")
            resolved = candidate.resolve(strict=False)
            forbidden = {Path("/"), Path.home().resolve()}
            if resolved in forbidden or resolved == self.root:
                raise ValueError(
                    "MOUNT_PATH must be a dedicated directory, not /, HOME, or VIBECANVAS_STORAGE_ROOT"
                )
            resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(resolved, 0o700)
            self.mount_path = resolved
        self.mount_sync_interval_seconds: float = float(
            os.environ.get("MOUNT_SYNC_INTERVAL_SECONDS") or 1.0
        )
        if not 0.25 <= self.mount_sync_interval_seconds <= 60.0:
            raise ValueError(
                "MOUNT_SYNC_INTERVAL_SECONDS must be between 0.25 and 60"
            )

        self.root.mkdir(parents=True, exist_ok=True)
        self.users.mkdir(parents=True, exist_ok=True)
        self.appendix.mkdir(parents=True, exist_ok=True)


class DatabaseConfig:
    """Postgres connection. URL from env, never committed."""

    def __init__(self, raw: Dict[str, Any]):
        self.url: str = (
            os.environ.get("DATABASE_URL")
            or raw.get("url")
            or "postgresql+asyncpg://dev:dev@localhost:5432/vibecanvas"
        )
        self.pool_size: int = int(raw.get("pool_size", 20))
        self.max_overflow: int = int(raw.get("max_overflow", 10))
        self.pool_recycle: int = int(raw.get("pool_recycle", 3600))


class RedisConfig:
    """Redis connection used for transient rate limits and event fan-out.

    URL from ``REDIS_URL`` env var or yaml; default points at a local
    dev instance. Durable background work is stored in PostgreSQL by DBOS;
    Redis is not a task broker or source of truth.
    """

    def __init__(self, raw: Dict[str, Any]):
        self.url: str = (
            os.environ.get("REDIS_URL") or raw.get("url") or "redis://localhost:6379/0"
        )


class ObjectStoreConfig:
    """Pluggable object store for batch results.

    Three providers:

    * ``"inmemory"`` — process-local dict. **Only correct single-process.**
      A blob ``put_bytes`` by one process is invisible to another, so it
      breaks any cross-process flow (KB indexing: api puts → worker fetches;
      batch download). Used by the test sandbox and single-process dev runs.
    * ``"filesystem"`` — blobs written under ``OBJECT_STORE_FS_ROOT``, a
      directory shared between api + background worker (a Docker
      named volume in compose; a shared dir in native multi-process runs).
      The OSS-standard local backend (LangFlow/Dify ship this before S3).
      **The recommended default for any multi-process deploy.**
    * ``"s3"`` — production. Requires ``S3_BUCKET`` + credentials and
      optionally ``S3_ENDPOINT_URL`` (when pointing at MinIO / a
      vendor-compatible service).

    Env > yaml > default for each field, matching ``DatabaseConfig`` /
    ``RedisConfig`` style.
    """

    def __init__(self, raw: Dict[str, Any]):
        self.provider: str = (
            os.environ.get("OBJECT_STORE_PROVIDER") or raw.get("provider") or "inmemory"
        )
        self.fs_root: str = (
            os.environ.get("OBJECT_STORE_FS_ROOT")
            or raw.get("fs_root")
            or "/var/lib/vibecanvas/objectstore"
        )
        self.fs_materialized_root: str = (
            os.environ.get("OBJECT_STORE_MATERIALIZED_ROOT")
            or raw.get("fs_materialized_root")
            or os.path.join(tempfile.gettempdir(), "vibecanvas-materialized")
        )
        self.fs_encryption_chunk_bytes: int = int(
            os.environ.get("OBJECT_STORE_ENCRYPTION_CHUNK_BYTES")
            or raw.get("fs_encryption_chunk_bytes")
            or 256 * 1024
        )
        if not 64 * 1024 <= self.fs_encryption_chunk_bytes <= 4 * 1024 * 1024:
            raise ValueError(
                "OBJECT_STORE_ENCRYPTION_CHUNK_BYTES must be between 64 KiB and 4 MiB"
            )
        self.s3_endpoint_url: str = (
            os.environ.get("S3_ENDPOINT_URL") or raw.get("s3_endpoint_url") or ""
        )
        self.s3_bucket: str = os.environ.get("S3_BUCKET") or raw.get("s3_bucket") or ""
        self.s3_access_key: str = (
            os.environ.get("S3_ACCESS_KEY") or raw.get("s3_access_key") or ""
        )
        self.s3_secret_key: str = (
            os.environ.get("S3_SECRET_KEY") or raw.get("s3_secret_key") or ""
        )
        self.s3_region: str = (
            os.environ.get("S3_REGION") or raw.get("s3_region") or "us-east-1"
        )
        self.s3_server_side_encryption: str = (
            os.environ.get("S3_SERVER_SIDE_ENCRYPTION")
            or raw.get("s3_server_side_encryption")
            or ""
        ).strip()
        self.s3_kms_key_id: str = (
            os.environ.get("S3_KMS_KEY_ID") or raw.get("s3_kms_key_id") or ""
        ).strip()


class WebSearchConfig:
    """Web search tool configuration.

    Provider selection and credentials. ``api_key`` must NOT be committed to
    config.yaml — supply ``WEB_SEARCH_API_KEY`` via the environment instead.

    Supported providers:
        "duckduckgo" — Free, no API key. Uses DuckDuckGo's HTML endpoint.
                       Default for the initial project.
        "tavily"     — AI-native, aggregates & ranks up to 20 sources.
                       API key from https://app.tavily.com.
        "brave"      — Independent index, $5/1k queries.
                       API key from https://api-dashboard.search.brave.com.

    ``provider`` defaults to "duckduckgo". Paid providers require
    WEB_SEARCH_API_KEY; missing key raises ToolError.
    """

    def __init__(self, raw: Dict[str, Any]):
        self.provider: str = (
            os.environ.get("WEB_SEARCH_PROVIDER") or raw.get("provider") or "duckduckgo"
        )
        self.api_key: str = (
            os.environ.get("WEB_SEARCH_API_KEY") or raw.get("api_key") or ""
        )
        self.max_results: int = int(raw.get("max_results", 5))
        self.timeout: int = int(raw.get("timeout", 10))


class McpConfig:
    """MCP (Model Context Protocol) loader limits — MCP T3.

    Three knobs guarding agent-build time when external MCP servers are
    connected and their tool lists are merged into the agent's tool set:

    * ``handshake_timeout_s`` — every ``MultiServerMCPClient.get_tools()``
      call is wrapped in ``asyncio.wait_for(timeout_s)``. A slow / dead
      server fails to that bound and is recorded as ``status='error: ...'``
      in the per-server health dict so the rest of the load keeps going.
    * ``per_server_tool_cap`` — a single server cannot contribute more
      than this many tools. Defends against a misconfigured server
      exporting hundreds of tools and blowing up the agent prompt.
    * ``per_tenant_tool_cap`` — total MCP tool budget across ALL of a
      tenant's enabled servers. Once exceeded, later servers are
      truncated / skipped with ``status='error: tenant tool budget
      exhausted'``.

    Defaults are conservative but workable for the common
    1-3-servers-per-tenant case.
    """

    def __init__(self, raw: Dict[str, Any]):
        self.handshake_timeout_s: float = float(
            os.environ.get("MCP_HANDSHAKE_TIMEOUT_S")
            or raw.get("handshake_timeout_s", 60.0)
        )
        self.per_server_tool_cap: int = int(raw.get("per_server_tool_cap", 50))
        self.per_tenant_tool_cap: int = int(raw.get("per_tenant_tool_cap", 200))
        # Private Host broker origin used for model, custom remote MCP, and
        # browser-control traffic. Built-in MCP facades themselves communicate
        # over the Agent Runtime control bus and do not expose an HTTP server.
        # This is intentionally independent of VIBECANVAS_PUBLIC_URL and
        # browser proxy prefixes. In a multi-pod deployment set it to the
        # pod-local/internal service origin.
        self.platform_internal_base_url: str = str(
            os.environ.get("PLATFORM_MCP_INTERNAL_BASE_URL")
            or raw.get("platform_internal_base_url")
            or "http://127.0.0.1:8000"
        ).rstrip("/")
        parts = urlsplit(self.platform_internal_base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(
                "PLATFORM_MCP_INTERNAL_BASE_URL must be an absolute HTTP(S) URL"
            )
        self.platform_capability_ttl_s: int = int(
            os.environ.get("PLATFORM_MCP_CAPABILITY_TTL_S")
            or raw.get("platform_capability_ttl_s", 24 * 60 * 60)
        )
        self.runtime_model_capability_ttl_s: int = int(
            os.environ.get("RUNTIME_MODEL_CAPABILITY_TTL_S")
            or raw.get("runtime_model_capability_ttl_s", 2 * 60 * 60)
        )
        if not 60 <= self.runtime_model_capability_ttl_s <= 24 * 60 * 60:
            raise ValueError(
                "RUNTIME_MODEL_CAPABILITY_TTL_S must be between 60 and 86400"
            )


class SkillsConfig:
    """Limits for user-provided immutable Skill bundles."""

    def __init__(self, raw: Dict[str, Any]):
        self.max_files: int = int(
            os.environ.get("SKILL_BUNDLE_MAX_FILES") or raw.get("max_files", 256)
        )
        self.max_file_bytes: int = int(
            os.environ.get("SKILL_FILE_MAX_BYTES")
            or raw.get("max_file_bytes", 2 * 1024 * 1024)
        )
        self.max_bundle_bytes: int = int(
            os.environ.get("SKILL_BUNDLE_MAX_BYTES")
            or raw.get("max_bundle_bytes", 16 * 1024 * 1024)
        )


class PublicUrlsConfig:
    """Canonical externally reachable application URL.

    OAuth providers must redirect to a stable public address, not an internal
    API port discovered from an incoming request. The same deployment-level
    base is shared by MCP account connections and future application-login
    providers, while each feature owns a separate callback path.
    """

    def __init__(self, raw: Dict[str, Any]):
        value = str(
            os.environ.get("VIBECANVAS_PUBLIC_URL") or raw.get("public_url") or ""
        ).strip()
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise ValueError(
                    "VIBECANVAS_PUBLIC_URL must be an absolute HTTP(S) URL"
                )
            value = urlunsplit(
                (parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")
            )
        self.public_url = value

    def absolute(self, path: str) -> str:
        if not self.public_url:
            raise ValueError(
                "VIBECANVAS_PUBLIC_URL is required for external OAuth callbacks"
            )
        return f"{self.public_url}/{path.lstrip('/')}"


class ObservabilityConfig:
    """Application-owned observability knobs. The OTEL_EXPORTER_*
    and OTEL_SERVICE_NAME vars are consumed natively by the OTel SDK, not here."""

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.log_level: str = (
            raw.get("log_level") or os.environ.get("LOG_LEVEL", "INFO")
        ).upper()
        self.log_format: str = raw.get("log_format") or os.environ.get(
            "LOG_FORMAT", "json"
        )
        self.metrics_enabled: bool = _as_bool(
            raw.get("metrics_enabled"), os.environ.get("METRICS_ENABLED"), default=True
        )
        self.otel_traces_enabled: bool = _as_bool(
            raw.get("otel_traces_enabled"),
            os.environ.get("OTEL_TRACES_ENABLED"),
            default=False,
        )


# ---------------------------------------------------------------------------
# AppConfig — top-level container
# ---------------------------------------------------------------------------


class VfsPathsConfig:
    """Tool-internal VFS paths — the 3rd layer of the storage stack
    (real storage → VFS → tool-internal paths). Tools map their outputs to these VFS
    paths; centralizing them here (instead of hardcoding across tools) removes the
    duplication and is a step toward cross-platform portability. The real-storage→VFS
    backend mapping (layer 1→2) is a SEPARATE, backend-layer concern — the agent-facing
    virtual scheme stays consistent across platforms.

    ``run_prefix`` is also the routing prefix the fs tools use to dispatch a path to
    the run-tier store (vs the main VFS), so it must be configured consistently."""

    def __init__(self, raw: Dict[str, Any]):
        self.run_prefix: str = (raw.get("run_prefix") or "/run").rstrip("/")
        # node_execute / run_workflow persist per-node results under this subdir.
        self.node_results_subdir: str = (
            raw.get("node_results_subdir") or "__exec__/nodes"
        ).strip("/")

    @property
    def node_results_dir(self) -> str:
        return f"{self.run_prefix}/{self.node_results_subdir}"

    def node_result_path(self, node_id: str) -> str:
        return f"{self.node_results_dir}/{node_id}.json"

    def is_run_path(self, path: str) -> bool:
        return isinstance(path, str) and path.startswith(self.run_prefix + "/")


class AppConfig:
    """Application-wide configuration singleton.

    Initialized once via ``AppConfig.load()``, then accessed as module-level
    ``config`` everywhere else.
    """

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw
        # Deployment profile is an explicit security boundary, not a logging
        # label. Development remains the default for source checkouts; a
        # production process must opt in and then pass the fail-closed startup
        # validator in ``security_profile.py``.
        self.environment: str = (
            str(
                os.environ.get("VIBECANVAS_ENV")
                or raw.get("environment")
                or "development"
            )
            .strip()
            .lower()
        )
        if self.environment not in {"development", "test", "production"}:
            raise ValueError(
                "VIBECANVAS_ENV must be one of development, test, production"
            )
        self.agent = AgentConfig(raw.get("agent") or {})
        self.runtime_model_egress_policy = str(
            os.environ.get("RUNTIME_MODEL_EGRESS_POLICY", "host")
        ).strip().lower()
        if self.runtime_model_egress_policy not in {"host", "public_https"}:
            raise ValueError(
                "RUNTIME_MODEL_EGRESS_POLICY must be host or public_https"
            )
        self.agent_runtime_types = _csv_choice_set(
            os.environ.get("AGENT_RUNTIME_TYPES"),
            default=("codex",),
            allowed=AVAILABLE_RUNTIME_TYPES,
            setting="AGENT_RUNTIME_TYPES",
        )
        self.codex_runtime_auth_methods = _csv_choice_set(
            os.environ.get("CODEX_RUNTIME_AUTH_METHODS"),
            default=("chatgpt", "managed_api", "personal_api"),
            allowed=frozenset({"chatgpt", "managed_api", "personal_api"}),
            setting="CODEX_RUNTIME_AUTH_METHODS",
        )
        self.codex_managed_apis = _codex_managed_apis_env(self.agent.model)
        storage_raw = raw.get("storage") or {}
        self.storage = StorageConfig(
            root_override=storage_raw.get("root"),
            vfs_upload_max_bytes=storage_raw.get("vfs_upload_max_bytes"),
        )
        # Hard cap applied to the bytes actually received from the ASGI server,
        # before multipart/JSON parsing. Keep this slightly above the default
        # 50 MiB upload limit to leave room for multipart framing. Operators
        # that raise VFS_UPLOAD_MAX_BYTES must deliberately raise this cap too.
        self.http_request_max_bytes: int = int(
            os.environ.get("VIBECANVAS_HTTP_REQUEST_MAX_BYTES")
            or raw.get("http_request_max_bytes")
            or 52 * 1024 * 1024
        )
        if not 1024 <= self.http_request_max_bytes <= 1024 * 1024 * 1024:
            raise ValueError(
                "VIBECANVAS_HTTP_REQUEST_MAX_BYTES must be between 1024 and 1073741824"
            )
        # Scan user-controlled files before they enter durable storage, KB
        # parsing, Preview, or a Runtime mount. Development/test keep an
        # explicit disabled mode; the production profile requires clamd over a
        # local Unix socket so file bytes do not cross an unauthenticated hop.
        self.upload_scanner_provider: str = (
            str(
                os.environ.get("UPLOAD_SCANNER_PROVIDER")
                or raw.get("upload_scanner_provider")
                or "disabled"
            )
            .strip()
            .lower()
        )
        self.upload_scanner_clamd_unix_socket: str = str(
            os.environ.get("UPLOAD_SCANNER_CLAMD_UNIX_SOCKET")
            or raw.get("upload_scanner_clamd_unix_socket")
            or ""
        ).strip()
        self.upload_scanner_timeout_seconds: float = float(
            os.environ.get("UPLOAD_SCANNER_TIMEOUT_SECONDS")
            or raw.get("upload_scanner_timeout_seconds")
            or 10.0
        )
        if self.upload_scanner_provider not in {"disabled", "clamd"}:
            raise ValueError("UPLOAD_SCANNER_PROVIDER must be disabled or clamd")
        if not 0.1 <= self.upload_scanner_timeout_seconds <= 120.0:
            raise ValueError(
                "UPLOAD_SCANNER_TIMEOUT_SECONDS must be between 0.1 and 120"
            )
        self.run_database_migrations: bool = _as_bool(
            raw.get("run_database_migrations"),
            os.environ.get("RUN_DATABASE_MIGRATIONS"),
            default=self.environment != "production",
        )
        self.database = DatabaseConfig(raw.get("database") or {})
        self.redis = RedisConfig(raw.get("redis") or {})
        self.dbos_system_database_url: str = (
            os.environ.get("DBOS_SYSTEM_DATABASE_URL")
            or self.database.url.replace(
                "postgresql+asyncpg://", "postgresql+psycopg://", 1
            )
        )
        self.dbos_run_migrations: bool = _as_bool(
            raw.get("dbos_run_migrations"),
            os.environ.get("DBOS_RUN_MIGRATIONS"),
            default=self.environment != "production",
        )
        self.dbos_client_pool_size: int = max(
            5, int(os.environ.get("DBOS_CLIENT_POOL_SIZE", "5"))
        )
        self.dbos_worker_pool_size: int = max(
            5, int(os.environ.get("DBOS_WORKER_POOL_SIZE", "5"))
        )
        self.dbos_max_executor_threads: int = max(
            1, int(os.environ.get("DBOS_MAX_EXECUTOR_THREADS", "2"))
        )
        self.background_queue_concurrency: int = max(
            1, int(os.environ.get("BACKGROUND_QUEUE_CONCURRENCY", "1"))
        )
        self.object_store = ObjectStoreConfig(raw.get("object_store") or {})
        self.web_search = WebSearchConfig(raw.get("web_search") or {})
        self.mcp = McpConfig(raw.get("mcp") or {})
        self.skills = SkillsConfig(raw.get("skills") or {})
        self.public_urls = PublicUrlsConfig(raw.get("public_urls") or {})
        self.observability = ObservabilityConfig(raw.get("observability") or {})
        self.vfs_paths = VfsPathsConfig(raw.get("vfs_paths") or {})
        self.agent_debug_view_enabled: bool = _as_bool(
            raw.get("agent_debug_view_enabled"),
            os.environ.get("AGENT_DEBUG_VIEW_ENABLED"),
            default=False,
        )
        self.enable_test_user: bool = _as_bool(
            raw.get("enable_test_user"),
            os.environ.get("ENABLE_TEST_USER"),
            default=False,
        )
        # Enterprise OIDC remains an opt-in business capability.  Keep one
        # server-owned flag for both the public login surface and the SSO
        # endpoints so hiding the button can never leave a callable backdoor.
        self.enterprise_sso_enabled: bool = _as_bool(
            raw.get("enterprise_sso_enabled"),
            os.environ.get("ENTERPRISE_SSO_ENABLED"),
            default=False,
        )
        # UX-10e — server-side signing secret for the VFS signed-URL raw-bytes
        # media endpoint (``/api/v1/vfs/raw``). The HMAC over the URL params is
        # keyed by this. MUST be set in production (a stable, secret value shared
        # by every API replica so a URL signed by one is verifiable by another).
        # env > yaml > a per-process random fallback. The random fallback keeps
        # dev/tests working with NO config, but signed URLs do NOT survive a
        # restart and are NOT valid across replicas — so prod MUST set
        # VIBECANVAS_SIGNING_SECRET. A `_signing_secret_is_ephemeral` flag lets
        # startup log a warning when the fallback is in use.
        _raw_signing = os.environ.get("VIBECANVAS_SIGNING_SECRET") or raw.get(
            "signing_secret"
        )
        self._signing_secret_is_ephemeral: bool = not _raw_signing
        self.signing_secret: str = _raw_signing or secrets.token_urlsafe(48)
        # Browser-automation (§15.A) — HMAC secret for the stateless, short-lived
        # scoped token the web app mints and hands to the browser extension. It
        # is browser/extension/audience/Session-generation bound, expires within
        # 15 minutes, and reconnect also verifies live Session state. MUST be
        # set to a stable secret in
        # production (shared by every API replica so a token minted by one is
        # verifiable by another). env > yaml > a dev-insecure fallback that keeps
        # dev/tests working with NO config. Mirrors RedisConfig's env-or-yaml shape.
        self.browser_token_secret: str = (
            os.environ.get("BROWSER_TOKEN_SECRET")
            or raw.get("browser_token_secret")
            or "dev-insecure-browser-secret-change-me"
        )
        # Published MV3 identity. It binds WebSocket capabilities and their
        # Origin checks to this extension, and must match the manifest's fixed
        # public key-derived id plus the Web `/embed` frame-ancestor policy.
        self.browser_extension_id: str = str(
            os.environ.get("VIBECANVAS_BROWSER_EXTENSION_ID")
            or raw.get("browser_extension_id")
            or "mkfldhmlgdbpmhplaphhcfcdcoaakcik"
        ).strip()
        # Sharing is an independent product gate; authorization itself is
        # always enforced by the pinned OpenFGA model.
        self.resource_sharing_enabled: bool = _as_bool(
            raw.get("resource_sharing_enabled"),
            os.environ.get("RESOURCE_SHARING_ENABLED"),
            default=False,
        )
        # Production high-risk mutations require a fresh phishing-resistant
        # WebAuthn step-up. Development/test can opt in explicitly so legacy
        # fixtures are not silently treated as elevated.
        self.high_risk_step_up_required: bool = _as_bool(
            raw.get("high_risk_step_up_required"),
            os.environ.get("HIGH_RISK_STEP_UP_REQUIRED"),
            default=False,
        )
        # Privileged support is deny-by-default and never inferred from an
        # organization role. This deployment-secret allowlist bootstraps the
        # reviewers who manage durable, expiring eligibility records; being in
        # the allowlist alone does not grant any customer support access.
        self.privileged_access_enabled: bool = _as_bool(
            raw.get("privileged_access_enabled"),
            os.environ.get("PRIVILEGED_ACCESS_ENABLED"),
            default=False,
        )
        raw_operator_ids = (
            os.environ.get("PRIVILEGED_SUPPORT_OPERATOR_IDS")
            or raw.get("privileged_support_operator_ids")
            or ""
        )
        if isinstance(raw_operator_ids, (list, tuple, set)):
            operator_values = [str(value) for value in raw_operator_ids]
        else:
            operator_values = str(raw_operator_ids).split(",")
        try:
            self.privileged_support_operator_ids: frozenset[str] = frozenset(
                str(uuid.UUID(value.strip()))
                for value in operator_values
                if value.strip()
            )
        except ValueError as exc:
            raise ValueError(
                "PRIVILEGED_SUPPORT_OPERATOR_IDS must contain UUIDs"
            ) from exc
        if (
            self.privileged_access_enabled
            and len(self.privileged_support_operator_ids) < 2
        ):
            raise ValueError(
                "privileged access requires at least two eligible operators"
            )
        self.privileged_access_bootstrap_admin_ids = (
            self.privileged_support_operator_ids
        )
        public_parts = urlsplit(
            self.public_urls.public_url or "http://localhost"
        )
        self.webauthn_rp_id: str = str(
            os.environ.get("WEBAUTHN_RP_ID")
            or raw.get("webauthn_rp_id")
            or public_parts.hostname
            or "localhost"
        ).strip().lower().rstrip(".")
        self.webauthn_origin: str = str(
            os.environ.get("WEBAUTHN_ORIGIN")
            or raw.get("webauthn_origin")
            or f"{public_parts.scheme}://{public_parts.netloc}"
        ).strip().rstrip("/")
        self.webauthn_rp_name: str = str(
            os.environ.get("WEBAUTHN_RP_NAME")
            or raw.get("webauthn_rp_name")
            or "Flowork"
        ).strip()
        rp_parts = urlsplit(self.webauthn_origin)
        if (
            not self.webauthn_rp_id
            or any(value in self.webauthn_rp_id for value in ("://", "/", ":"))
        ):
            raise ValueError("WEBAUTHN_RP_ID must be a hostname without a port")
        if (
            rp_parts.scheme not in {"http", "https"}
            or not rp_parts.netloc
            or rp_parts.path not in {"", "/"}
            or rp_parts.query
            or rp_parts.fragment
        ):
            raise ValueError("WEBAUTHN_ORIGIN must be an exact HTTP(S) origin")
        openfga_bootstrap = _openfga_bootstrap_config()
        self.openfga_api_url: str = str(
            os.environ.get("OPENFGA_API_URL") or raw.get("openfga_api_url") or ""
        ).strip()
        self.openfga_store_id: str = str(
            os.environ.get("OPENFGA_STORE_ID")
            or raw.get("openfga_store_id")
            or openfga_bootstrap.get("store_id")
            or ""
        ).strip()
        self.openfga_authorization_model_id: str = str(
            os.environ.get("OPENFGA_AUTHORIZATION_MODEL_ID")
            or raw.get("openfga_authorization_model_id")
            or openfga_bootstrap.get("authorization_model_id")
            or ""
        ).strip()
        self.openfga_api_token: str = str(
            os.environ.get("OPENFGA_API_TOKEN") or raw.get("openfga_api_token") or ""
        ).strip()
        self.openfga_timeout_seconds: float = float(
            os.environ.get("OPENFGA_TIMEOUT_SECONDS")
            or raw.get("openfga_timeout_seconds")
            or 2.0
        )
        if not 0.05 <= self.openfga_timeout_seconds <= 30.0:
            raise ValueError("OPENFGA_TIMEOUT_SECONDS must be between 0.05 and 30")
        self.openfga_erasure_database_url: str = str(
            os.environ.get("OPENFGA_ERASURE_DATABASE_URL")
            or raw.get("openfga_erasure_database_url")
            or ""
        ).strip()
        self.kms_provider: str = str(
            os.environ.get("KMS_PROVIDER") or raw.get("kms_provider") or ""
        ).strip()
        self.kms_key_id: str = str(
            os.environ.get("KMS_KEY_ID") or raw.get("kms_key_id") or ""
        ).strip()
        self.kms_workload_identity: str = str(
            os.environ.get("KMS_WORKLOAD_IDENTITY")
            or raw.get("kms_workload_identity")
            or ""
        ).strip()
        # Production uses the cloud SDK's workload-identity chain. Retain only
        # presence metadata so startup can reject long-lived static AWS keys
        # without copying them onto the application config object.
        self.aws_static_credentials_present: bool = bool(
            os.environ.get("AWS_ACCESS_KEY_ID")
            or os.environ.get("AWS_SECRET_ACCESS_KEY")
        )
        self.kms_local_master_key: str = str(
            os.environ.get("KMS_LOCAL_MASTER_KEY")
            or raw.get("kms_local_master_key")
            or ""
        ).strip()
        self.kms_local_master_key_file: str = str(
            os.environ.get("KMS_LOCAL_MASTER_KEY_FILE")
            or raw.get("kms_local_master_key_file")
            or ""
        ).strip()
        self.content_lookup_hmac_key_file: str = str(
            os.environ.get("CONTENT_LOOKUP_HMAC_KEY_FILE")
            or raw.get("content_lookup_hmac_key_file")
            or ""
        ).strip()
        content_lookup_hmac_key = str(
            os.environ.get("CONTENT_LOOKUP_HMAC_KEY")
            or raw.get("content_lookup_hmac_key")
            or ""
        ).strip()
        if not content_lookup_hmac_key and self.content_lookup_hmac_key_file:
            try:
                content_lookup_hmac_key = (
                    Path(self.content_lookup_hmac_key_file)
                    .read_text(encoding="utf-8")
                    .strip()
                )
            except OSError as exc:
                raise ValueError(
                    "CONTENT_LOOKUP_HMAC_KEY_FILE could not be read"
                ) from exc
        self.content_lookup_hmac_key: str = content_lookup_hmac_key or (
            self.signing_secret if self.environment in {"development", "test"} else ""
        )
        self.smtp_host: str = str(
            os.environ.get("SMTP_HOST") or raw.get("smtp_host") or ""
        ).strip()
        self.smtp_user: str = str(
            os.environ.get("SMTP_USER") or raw.get("smtp_user") or ""
        ).strip()
        self.smtp_password_secret_id: str = str(
            os.environ.get("SMTP_PASSWORD_SECRET_ID")
            or raw.get("smtp_password_secret_id")
            or ""
        ).strip()
        # Do not retain the value on the config object; production validation
        # needs only enough information to reject the plaintext mechanism.
        self.smtp_plaintext_password_present: bool = bool(
            os.environ.get("SMTP_PASSWORD")
        )
        self.audit_export_url: str = str(
            os.environ.get("AUDIT_EXPORT_URL") or raw.get("audit_export_url") or ""
        ).strip()
        self.backup_encryption_verified: bool = _as_bool(
            raw.get("backup_encryption_verified"),
            os.environ.get("BACKUP_ENCRYPTION_VERIFIED"),
            default=False,
        )
        self.purge_worker_enabled: bool = _as_bool(
            raw.get("purge_worker_enabled"),
            os.environ.get("PURGE_WORKER_ENABLED"),
            default=True,
        )
        self.account_deletion_mode: str = str(
            os.environ.get("ACCOUNT_DELETION_MODE")
            or raw.get("account_deletion_mode")
            or "immediate"
        ).strip().lower()
        if self.account_deletion_mode not in {"immediate", "delayed"}:
            raise ValueError(
                "ACCOUNT_DELETION_MODE must be 'immediate' or 'delayed'"
            )
        try:
            self.account_deletion_retention_days: int = int(
                os.environ.get("ACCOUNT_DELETION_RETENTION_DAYS")
                or raw.get("account_deletion_retention_days")
                or 14
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "ACCOUNT_DELETION_RETENTION_DAYS must be an integer"
            ) from exc
        if not 1 <= self.account_deletion_retention_days <= 365:
            raise ValueError(
                "ACCOUNT_DELETION_RETENTION_DAYS must be between 1 and 365"
            )
        self.distributed_auth_rate_limit_enabled: bool = _as_bool(
            raw.get("distributed_auth_rate_limit_enabled"),
            os.environ.get("DISTRIBUTED_AUTH_RATE_LIMIT_ENABLED"),
            default=False,
        )
        self.web_session_cookie_enabled: bool = _as_bool(
            raw.get("web_session_cookie_enabled"),
            os.environ.get("WEB_SESSION_COOKIE_ENABLED"),
            default=False,
        )
        cookie_secure_value = os.environ.get("WEB_SESSION_COOKIE_SECURE")
        if cookie_secure_value is None:
            cookie_secure_value = raw.get("web_session_cookie_secure")
        self.web_session_cookie_secure: bool | None = (
            None
            if cookie_secure_value is None or str(cookie_secure_value).strip() == ""
            else _as_bool(None, cookie_secure_value, default=False)
        )
        self.extension_scoped_token_enabled: bool = _as_bool(
            raw.get("extension_scoped_token_enabled"),
            os.environ.get("EXTENSION_SCOPED_TOKEN_ENABLED"),
            default=False,
        )
        self.security_frame_ancestors: str = str(
            os.environ.get("VIBECANVAS_FRAME_ANCESTORS")
            or raw.get("security_frame_ancestors")
            or ""
        ).strip()
        trusted_proxy_value = (
            os.environ.get("TRUSTED_PROXY_CIDRS")
            or raw.get("trusted_proxy_cidrs")
            or ""
        )
        if isinstance(trusted_proxy_value, str):
            self.trusted_proxy_cidrs: tuple[str, ...] = tuple(
                item.strip() for item in trusted_proxy_value.split(",") if item.strip()
            )
        else:
            self.trusted_proxy_cidrs = tuple(
                str(item).strip() for item in trusted_proxy_value if str(item).strip()
            )
        try:
            self.trusted_proxy_cidrs = tuple(
                str(ipaddress.ip_network(value, strict=False))
                for value in self.trusted_proxy_cidrs
            )
        except ValueError as exc:
            raise ValueError("TRUSTED_PROXY_CIDRS contains an invalid network") from exc
        # Browser side-panel lease recovery. A transient transport loss keeps
        # the chat/window lease for this many seconds so the extension can
        # reconnect and submit a session snapshot. After the grace window the
        # durable chat projection is lazily reconciled to ``inactive``. Keep the
        # value shared by every API replica through environment/config.
        self.browser_lost_grace_seconds: int = max(
            1,
            int(
                os.environ.get("BROWSER_LOST_GRACE_SECONDS")
                or raw.get("browser_lost_grace_seconds")
                or 30
            ),
        )
        # Forward-compat #3 — cluster role; affects which queues workers
        # subscribe to.
        #   'monolith' (default) — single API + single worker fleet
        #   'control_plane'      — API only, no worker
        #   'data_plane'         — worker only, no HTTP routes
        self.cluster_role: str = os.environ.get("CLUSTER_ROLE", "monolith")
        # RE-6 P1 — path to the ``runsc`` (gVisor) binary for the OS-sandbox
        # provider. Located at runtime (env > yaml > PATH); not a pip dep.
        self.runsc_path: str | None = (
            os.environ.get("RUNSC_PATH") or raw.get("runsc_path") or None
        )
        # Sandbox control-plane ownership. ``service`` keeps every resident
        # gVisor process and broker in the separately supervised sandboxd
        # process; API and background-worker processes hold serializable proxies.
        # ``embedded`` remains available for isolated unit tests only.
        self.sandbox_service_mode: str = (
            os.environ.get("SANDBOX_SERVICE_MODE")
            or raw.get("sandbox_service_mode")
            or "service"
        ).strip().lower()
        self.sandbox_service_socket: str = (
            os.environ.get("SANDBOX_SERVICE_SOCKET")
            or raw.get("sandbox_service_socket")
            or os.path.join(tempfile.gettempdir(), "vibecanvas-sandboxd.sock")
        )
        self.sandbox_service_socket_mode: int = _unix_permission_mode(
            os.environ.get("SANDBOX_SERVICE_SOCKET_MODE")
            or raw.get("sandbox_service_socket_mode"),
            default=0o600,
            setting="SANDBOX_SERVICE_SOCKET_MODE",
        )
        self.sandbox_service_socket_dir_mode: int = _unix_permission_mode(
            os.environ.get("SANDBOX_SERVICE_SOCKET_DIR_MODE")
            or raw.get("sandbox_service_socket_dir_mode"),
            default=0o700,
            setting="SANDBOX_SERVICE_SOCKET_DIR_MODE",
        )
        self.sandbox_service_socket_gid: int = int(
            os.environ.get("SANDBOX_SERVICE_SOCKET_GID")
            or raw.get("sandbox_service_socket_gid")
            or -1
        )
        if self.sandbox_service_socket_gid < -1:
            raise ValueError("SANDBOX_SERVICE_SOCKET_GID must be -1 or a non-negative GID")
        self.sandbox_service_endpoint: str = (
            os.environ.get("SANDBOX_SERVICE_ENDPOINT")
            or raw.get("sandbox_service_endpoint")
            or f"unix://{self.sandbox_service_socket}"
        )
        self.sandbox_service_ca_file: str = (
            os.environ.get("SANDBOX_SERVICE_CA_FILE")
            or raw.get("sandbox_service_ca_file")
            or ""
        )
        self.sandbox_service_cert_file: str = (
            os.environ.get("SANDBOX_SERVICE_CERT_FILE")
            or raw.get("sandbox_service_cert_file")
            or ""
        )
        self.sandbox_service_key_file: str = (
            os.environ.get("SANDBOX_SERVICE_KEY_FILE")
            or raw.get("sandbox_service_key_file")
            or ""
        )
        self.sandbox_service_connect_timeout_s: float = float(
            os.environ.get("SANDBOX_SERVICE_CONNECT_TIMEOUT_S")
            or raw.get("sandbox_service_connect_timeout_s")
            or 5.0
        )
        # Upper bound for long control-plane jobs whose result is returned as a
        # single response (for example a deployment workflow invocation).
        # Interactive agent/workflow execution uses the streaming RPC instead.
        # This is intentionally generous and operator-configurable so the
        # transport does not impose an accidental 120-second product limit.
        self.sandbox_service_operation_timeout_s: float = float(
            os.environ.get("SANDBOX_SERVICE_OPERATION_TIMEOUT_S")
            or raw.get("sandbox_service_operation_timeout_s")
            or 86400.0
        )
        if self.sandbox_service_operation_timeout_s < 60.0:
            raise ValueError(
                "SANDBOX_SERVICE_OPERATION_TIMEOUT_S must be at least 60 seconds"
            )
        # Gate the host↔sandbox-bus debug-execute path. When enabled and
        # a workflow is sandbox-runnable (``classify_workflow``), a debug run goes
        # through the gVisor sandbox and UDS bus broker. Environment configuration
        # takes precedence over YAML; the feature is disabled by default.
        self.sandbox_debug_execute_enabled: bool = _as_bool(
            raw.get("sandbox_debug_execute_enabled"),
            os.environ.get("SANDBOX_DEBUG_EXECUTE_ENABLED"),
            default=False,
        )
        # P1 — host-level cap on CONCURRENT sandbox instances (admission control).
        # The only cap today is WarmPoolManager.max_workers, which the warm-pool
        # retirement removes; this is its replacement at the Sandbox layer.
        # env > yaml > default(8).
        self.sandbox_max_concurrent: int = int(
            os.environ.get("SANDBOX_MAX_CONCURRENT")
            or raw.get("sandbox_max_concurrent")
            or 8
        )
        # The ``execute_in_sandbox`` kill-switch was removed: the gVisor
        # sandbox is now the unconditional, sole workflow-execution path for the
        # sync (batch/deploy) runner. There is no in-process fallback to toggle.
        # gVisor network mode passed to runsc ``--network=``.
        # Default ``"host"`` keeps the validated rootless-dev behavior byte for
        # byte. Rootless development supports ``host``; validate the runsc
        # netstack separately for a rootful server environment.
        self.sandbox_network: str = (
            os.environ.get("SANDBOX_NETWORK") or raw.get("sandbox_network") or "host"
        )
        # gVisor execution platform. systrap is the production/default fast
        # path. ptrace is an explicit compatibility option for nested runtimes
        # (for example WSL processes already constrained by outer seccomp)
        # where systrap cannot create its syscall thread.
        self.sandbox_gvisor_platform: str = (
            os.environ.get("SANDBOX_GVISOR_PLATFORM")
            or raw.get("sandbox_gvisor_platform")
            or "systrap"
        ).strip().lower()
        if self.sandbox_gvisor_platform not in {"systrap", "ptrace"}:
            raise ValueError(
                "SANDBOX_GVISOR_PLATFORM must be systrap or ptrace"
            )
        # Plan-B egress (B5) — how the sandbox reaches the network.
        # ``"host-network"`` (DEFAULT/dev) keeps today's behavior byte-for-byte:
        # the sandbox shares the host network (``--network=host``), NO proxy, NO
        # broker. ``"proxy"`` (prod) forces ``--network=none`` and tunnels every
        # outbound HTTP(S) over the per-run host EgressBroker (allowlist relay)
        # via the in-sandbox forward proxy. env > yaml > default.
        self.sandbox_egress_mode: str = (
            os.environ.get("SANDBOX_EGRESS_MODE")
            or raw.get("sandbox_egress_mode")
            or "host-network"
        )
        if self.sandbox_egress_mode not in {"host-network", "proxy"}:
            raise ValueError(
                "SANDBOX_EGRESS_MODE must be host-network or proxy"
            )
        # One public-egress policy applies to every sandbox workload. Lifecycle
        # (one-shot, resident, snapshot) never changes network semantics.
        # Legacy SANDBOX_AGENT_* names remain read-only compatibility aliases so
        # existing deployments upgrade without silently changing authority.
        self.sandbox_egress_policy: str = (
            os.environ.get("SANDBOX_EGRESS_POLICY")
            or raw.get("sandbox_egress_policy")
            or os.environ.get("SANDBOX_AGENT_EGRESS_POLICY")
            or raw.get("sandbox_agent_egress_policy")
            or "public"
        ).strip().lower()
        if self.sandbox_egress_policy not in {
            "public",
            "allowlist",
            "platform-only",
        }:
            raise ValueError(
                "SANDBOX_EGRESS_POLICY must be public, allowlist, or platform-only"
            )
        self.sandbox_egress_allow_hosts: tuple[str, ...] = _csv_values(
            os.environ.get("SANDBOX_EGRESS_ALLOW_HOSTS")
            or raw.get("sandbox_egress_allow_hosts")
            or os.environ.get("SANDBOX_AGENT_EGRESS_ALLOW_HOSTS")
            or raw.get("sandbox_agent_egress_allow_hosts")
        )
        self.sandbox_egress_private_targets: tuple[tuple[str, int], ...] = (
            _private_egress_targets(
                os.environ.get("SANDBOX_EGRESS_PRIVATE_TARGETS")
                or raw.get("sandbox_egress_private_targets")
                or os.environ.get("SANDBOX_AGENT_EGRESS_PRIVATE_TARGETS")
                or raw.get("sandbox_agent_egress_private_targets")
            )
        )
        self.sandbox_egress_trusted_proxy_cidrs: tuple[str, ...] = (
            _trusted_proxy_cidrs(
                os.environ.get("SANDBOX_EGRESS_TRUSTED_PROXY_CIDRS")
                or raw.get("sandbox_egress_trusted_proxy_cidrs")
            )
        )
        # Host-side catalog/metadata clients never inherit HTTP_PROXY. An
        # explicit operator setting keeps proxy behavior deterministic and
        # prevents process environment changes from weakening SSRF controls.
        self.control_plane_http_proxy: str = _control_plane_http_proxy(
            os.environ.get("FLOWORK_CONTROL_PLANE_HTTP_PROXY", "")
            or raw.get("control_plane_http_proxy")
        )
        if (
            self.sandbox_egress_policy == "allowlist"
            and not self.sandbox_egress_allow_hosts
        ):
            raise ValueError(
                "SANDBOX_EGRESS_ALLOW_HOSTS is required for allowlist policy"
            )
        # Compatibility attributes for third-party integrations that imported
        # the former Runtime-only setting. New code must use the unified names.
        self.sandbox_agent_egress_policy = self.sandbox_egress_policy
        self.sandbox_agent_egress_allow_hosts = self.sandbox_egress_allow_hosts
        self.sandbox_agent_egress_private_targets = self.sandbox_egress_private_targets
        # One startup selector owns both privilege and lifecycle policy. This
        # prevents incompatible combinations such as rootless + snapshot from
        # being assembled through several independent environment variables.
        sandbox_profiles = {
            "rootless-warm": (False, "coldboot"),
            "rootful-warm": (True, "coldboot"),
            "rootful-snapshot": (True, "snapshot"),
        }
        explicit_sandbox_type = os.environ.get("SANDBOX_TYPE")
        self.sandbox_type: str = (
            explicit_sandbox_type or raw.get("sandbox_type") or "rootless-warm"
        ).strip().lower()
        if self.sandbox_type not in sandbox_profiles:
            raise ValueError(
                "SANDBOX_TYPE must be one of: "
                + ", ".join(sorted(sandbox_profiles))
            )
        self.sandbox_rootful, profile_resident_mode = sandbox_profiles[self.sandbox_type]
        snapshot_root = Path(
            os.environ.get("SANDBOX_SNAPSHOT_ROOT")
            or raw.get("sandbox_snapshot_root")
            or "/var/lib/vibecanvas/snapshots"
        ).expanduser()
        if not snapshot_root.is_absolute() or snapshot_root == Path("/"):
            raise ValueError("SANDBOX_SNAPSHOT_ROOT must be a dedicated absolute path")
        self.sandbox_snapshot_root: str = str(snapshot_root)
        self.sandbox_snapshot_ready_timeout_s: float = float(
            os.environ.get("SANDBOX_SNAPSHOT_READY_TIMEOUT_S")
            or raw.get("sandbox_snapshot_ready_timeout_s")
            or 120.0
        )
        if self.sandbox_snapshot_ready_timeout_s <= 0:
            raise ValueError("SANDBOX_SNAPSHOT_READY_TIMEOUT_S must be positive")
        self.sandbox_snapshot_checkpoint_timeout_s: float = float(
            os.environ.get("SANDBOX_SNAPSHOT_CHECKPOINT_TIMEOUT_S")
            or raw.get("sandbox_snapshot_checkpoint_timeout_s")
            or 120.0
        )
        self.sandbox_snapshot_restore_timeout_s: float = float(
            os.environ.get("SANDBOX_SNAPSHOT_RESTORE_TIMEOUT_S")
            or raw.get("sandbox_snapshot_restore_timeout_s")
            or 120.0
        )
        for setting, value in (
            (
                "SANDBOX_SNAPSHOT_CHECKPOINT_TIMEOUT_S",
                self.sandbox_snapshot_checkpoint_timeout_s,
            ),
            (
                "SANDBOX_SNAPSHOT_RESTORE_TIMEOUT_S",
                self.sandbox_snapshot_restore_timeout_s,
            ),
        ):
            if not 1.0 <= value <= 900.0:
                raise ValueError(f"{setting} must be between 1 and 900 seconds")
        self.sandbox_snapshot_compression: str = str(
            os.environ.get("SANDBOX_SNAPSHOT_COMPRESSION")
            or raw.get("sandbox_snapshot_compression")
            or "none"
        ).strip().lower()
        if self.sandbox_snapshot_compression not in {"none", "flate-best-speed"}:
            raise ValueError(
                "SANDBOX_SNAPSHOT_COMPRESSION must be none or flate-best-speed"
            )

        # Interactive-session snapshot lifecycle. This applies equally to Chat
        # turns and Workflow-page Run/Node Debug sessions. These are durations
        # spent in each state, not cumulative ages:
        #   warm --SANDBOX_WARM_IDLE_TTL_S--> hibernated snapshot
        #   hibernated --SANDBOX_SNAPSHOT_IDLE_TTL_S--> fully released
        # A separate TTL owns the credential-free baseline used by one-shot
        # webhook, schedule, deployment and other background Workflow runs.
        # The legacy SANDBOX_IDLE_TTL_S remains a non-snapshot compatibility
        # alias and may not be combined with the rootful-snapshot profile.
        legacy_idle_ttl = os.environ.get("SANDBOX_IDLE_TTL_S")
        if explicit_sandbox_type and self.sandbox_type == "rootful-snapshot" and legacy_idle_ttl:
            raise ValueError(
                "SANDBOX_IDLE_TTL_S conflicts with SANDBOX_TYPE='rootful-snapshot'; "
                "use SANDBOX_WARM_IDLE_TTL_S and SANDBOX_SNAPSHOT_IDLE_TTL_S"
            )
        self.sandbox_warm_idle_ttl_s: int = int(
            os.environ.get("SANDBOX_WARM_IDLE_TTL_S")
            or raw.get("sandbox_warm_idle_ttl_s")
            or (300 if self.sandbox_type == "rootful-snapshot" else 1800)
        )
        self.sandbox_snapshot_idle_ttl_s: int = int(
            os.environ.get("SANDBOX_SNAPSHOT_IDLE_TTL_S")
            or raw.get("sandbox_snapshot_idle_ttl_s")
            or 1800
        )
        self.sandbox_workflow_snapshot_ttl_s: int = int(
            os.environ.get("SANDBOX_WORKFLOW_SNAPSHOT_TTL_S")
            or raw.get("sandbox_workflow_snapshot_ttl_s")
            or 86400
        )
        # The in-sandbox job server publishes activity facts only. sandboxd
        # observes them and measures elapsed silence on its own monotonic clock;
        # no TTL countdown is owned by untrusted sandbox code.
        self.sandbox_activity_poll_interval_s: float = float(
            os.environ.get("SANDBOX_ACTIVITY_POLL_INTERVAL_S")
            or raw.get("sandbox_activity_poll_interval_s")
            or 5.0
        )
        if not 0.5 <= self.sandbox_activity_poll_interval_s <= 60.0:
            raise ValueError(
                "SANDBOX_ACTIVITY_POLL_INTERVAL_S must be between 0.5 and 60 seconds"
            )
        for setting, value in (
            ("SANDBOX_WARM_IDLE_TTL_S", self.sandbox_warm_idle_ttl_s),
            ("SANDBOX_SNAPSHOT_IDLE_TTL_S", self.sandbox_snapshot_idle_ttl_s),
            (
                "SANDBOX_WORKFLOW_SNAPSHOT_TTL_S",
                self.sandbox_workflow_snapshot_ttl_s,
            ),
        ):
            if value <= 0:
                raise ValueError(f"{setting} must be a positive integer")
        self.sandbox_idle_ttl_s: int = int(
            legacy_idle_ttl
            or raw.get("sandbox_idle_ttl_s")
            or self.sandbox_warm_idle_ttl_s
        )
        if self.sandbox_idle_ttl_s <= 0:
            raise ValueError("SANDBOX_IDLE_TTL_S must be a positive integer")

        # Bound both snapshot disk growth and OCI mount fan-out. A snapshot
        # implementation must reject new state instead of silently exceeding
        # either operational limit.
        self.sandbox_snapshot_max_count: int = int(
            os.environ.get("SANDBOX_SNAPSHOT_MAX_COUNT")
            or raw.get("sandbox_snapshot_max_count")
            or 128
        )
        self.sandbox_snapshot_max_bytes: int = int(
            os.environ.get("SANDBOX_SNAPSHOT_MAX_BYTES")
            or raw.get("sandbox_snapshot_max_bytes")
            or 20 * 1024 * 1024 * 1024
        )
        self.sandbox_max_mounts: int = int(
            os.environ.get("SANDBOX_MAX_MOUNTS")
            or raw.get("sandbox_max_mounts")
            or 24
        )
        if self.sandbox_snapshot_max_count <= 0:
            raise ValueError("SANDBOX_SNAPSHOT_MAX_COUNT must be positive")
        if self.sandbox_snapshot_max_bytes < 64 * 1024 * 1024:
            raise ValueError("SANDBOX_SNAPSHOT_MAX_BYTES must be at least 67108864")
        if not 8 <= self.sandbox_max_mounts <= 128:
            raise ValueError("SANDBOX_MAX_MOUNTS must be between 8 and 128")
        # Agent-visible sandbox file/shell worker slots per resident session.
        # Each slot is a gVisor serve worker with its own job channel but the
        # same mounted /data,/memory,/logs,/mount and temporary /run.
        self.sandbox_fileop_workers: int = int(
            os.environ.get("SANDBOX_FILEOP_WORKERS")
            or raw.get("sandbox_fileop_workers")
            or 16
        )
        # Host Python package/source paths exposed to gVisor sandboxes.
        # Colon-separated, same shape as PYTHONPATH. Use this on deployments
        # where packages live outside sys.prefix/site-packages, for example:
        # SANDBOX_PYTHON_PATHS=/opt/venv/lib/python3.11/site-packages:/app/api/src
        self.sandbox_python_paths: list[str] = [
            p
            for p in (
                os.environ.get("SANDBOX_PYTHON_PATHS")
                or raw.get("sandbox_python_paths")
                or ""
            ).split(os.pathsep)
            if p
        ]
        # Max time to wait when retiring/remounting a resident agent sandbox.
        # Slow worker teardown must not block a replacement runtime or leave
        # frontend tool calls spinning indefinitely.
        self.sandbox_session_close_timeout_s: float = float(
            os.environ.get("SANDBOX_SESSION_CLOSE_TIMEOUT_S")
            or raw.get("sandbox_session_close_timeout_s")
            or 5.0
        )
        # ``sandbox_max_resident`` — global cap on CONCURRENT resident sandboxes
        # (admission control for the resident fleet). env > yaml > default(8).
        self.sandbox_max_resident: int = int(
            os.environ.get("SANDBOX_MAX_RESIDENT")
            or raw.get("sandbox_max_resident")
            or 8
        )
        # ``sandbox_resident_mode`` — how a resident sandbox is (re)started:
        # ``"coldboot"`` (DEFAULT) = fresh boot per session; ``"snapshot"`` =
        # restore from a checkpoint (the online fast-resume optimization, gated on
        # gVisor checkpoint/restore — not in rootless dev). env > yaml > default.
        self.sandbox_resident_mode: str = profile_resident_mode
        # ``agent_overlay_root`` — host scratch root where a per-agent env overlay
        # (declared pip libs etc.) is materialized before being bound into the
        # resident sandbox. This is rebuildable scratch, not durable user VFS.
        self.agent_overlay_root: str = (
            os.environ.get("AGENT_OVERLAY_ROOT")
            or raw.get("agent_overlay_root")
            or os.path.join(tempfile.gettempdir(), "vibecanvas-agent-overlay")
        )
        # Per-user root for Chat-scoped Agent Runtime state. Codex thread state
        # lives under a v2 per-Chat namespace. Optional ChatGPT account auth is
        # kept in a separate account-only subtree and mounted only for explicit
        # account turns; provider API keys never live here. Production mounts
        # this on a private durable volume. The namespace never appears in the
        # frontend or ordinary VFS contracts.
        self.agent_runtime_root: str = (
            os.environ.get("AGENT_RUNTIME_ROOT")
            or raw.get("agent_runtime_root")
            or os.path.join(tempfile.gettempdir(), "vibecanvas-agent-runtime")
        )
        # Durable POSIX volumes mounted directly into filesystem-backed Agent
        # Runtimes. This is a VFS control-plane backend, not the Object Store:
        # SQLite and other Runtime-owned files retain native locking, mmap, and
        # atomic-rename semantics. Defaulting to the legacy Runtime root enables
        # an in-place, one-time layout migration without losing existing Chats.
        self.vfs_volume_root: str = (
            os.environ.get("VFS_VOLUME_ROOT")
            or raw.get("vfs_volume_root")
            or self.agent_runtime_root
        )
        # Official Codex CLI used by the Codex runtime adapter and account-aware
        # capability discovery. An explicit path is important for services whose
        # PATH differs from the interactive shell (systemd, containers, workers).
        self.codex_cli_path: str = (
            os.environ.get("CODEX_CLI_PATH") or raw.get("codex_cli_path") or ""
        )
        # ``lib_overlay_root`` — host scratch root for the GLOBAL, content-
        # addressed CodeNode library overlays (the shared dep-set cache built
        # once and reused across runs/tenants, keyed by a sha256 of the declared
        # requirements). Unlike ``agent_overlay_root`` (a per-agent scratch) this
        # is the durable, shared overlay STORE; it is built host-side and NEVER
        # lives inside a run sandbox. Mirrors the same env > yaml > tempdir
        # default shape. env > yaml > tempdir default.
        self.lib_overlay_root: str = (
            os.environ.get("LIB_OVERLAY_ROOT")
            or raw.get("lib_overlay_root")
            or os.path.join(tempfile.gettempdir(), "vibecanvas-lib-overlay")
        )

    @classmethod
    def load(cls, config_path: Optional[Path | str] = None) -> "AppConfig":
        path = Path(config_path or _CONFIG_PATH)
        raw: Dict[str, Any] = {}
        if path.is_file():
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                print(f"[app_config] failed to parse {path}: {e}")
        return cls(raw)

    @property
    def raw(self) -> Dict[str, Any]:
        """Access the original parsed yaml dict (for backward compat)."""
        return self._raw


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

config: AppConfig = AppConfig.load()
