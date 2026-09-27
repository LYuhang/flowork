"""User-scoped ChatGPT authentication for the optional Codex account mode.

The account cache is deliberately separate from every Chat's Codex state.
The official ``auth.json`` is staged into one Chat's private Runtime at process
startup and reconciled at a quiescent boundary, so Codex can atomically refresh
credentials without exposing another Chat's thread database.
"""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.codex_app_server import (
    _CODEX_FILE_CREDENTIAL_CONFIG,
    CodexAppServer,
    CodexAppServerError,
)
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeModelOption,
    RuntimeReasoningEffortOption,
)
from vibecanvas_api.services.codex_cli import resolve_codex_executable


def _safe_component(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or any(character in value for character in ("/", "\\", "\0"))
    ):
        raise ValueError("invalid runtime identity")
    return value


def codex_account_auth_home(tenant_id: str, user_id: str) -> str:
    """Return the private account-only home; it never stores thread state."""
    tenant = _safe_component(str(tenant_id))
    user = _safe_component(str(user_id))
    return os.path.join(
        config.agent_runtime_root,
        tenant,
        user,
        "codex-account-v1",
        ".codex",
    )


def codex_account_auth_file(tenant_id: str, user_id: str) -> str:
    return os.path.join(codex_account_auth_home(tenant_id, user_id), "auth.json")


@dataclass(frozen=True)
class CodexAccountStatus:
    cli_available: bool
    authenticated: bool


@dataclass(frozen=True)
class CodexDeviceLogin:
    login_session_id: str
    verification_url: str
    user_code: str
    expires_at: str


def _bounded_text(value: Any, *, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return int(value)


def _percentage(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return round(min(100.0, max(0.0, float(value))), 2)


def _normalize_rate_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used_percent = _percentage(value.get("usedPercent"))
    if used_percent is None:
        return None
    return {
        "used_percent": used_percent,
        "window_duration_mins": _nonnegative_int(value.get("windowDurationMins")),
        "resets_at": _nonnegative_int(value.get("resetsAt")),
    }


def _normalize_rate_bucket(
    value: Any,
    *,
    fallback_id: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    primary = _normalize_rate_window(value.get("primary"))
    secondary = _normalize_rate_window(value.get("secondary"))
    if primary is None and secondary is None:
        return None
    credits = value.get("credits")
    normalized_credits = None
    if isinstance(credits, dict):
        normalized_credits = {
            "has_credits": bool(credits.get("hasCredits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": _bounded_text(credits.get("balance"), limit=64),
        }
    individual = value.get("individualLimit")
    normalized_individual = None
    if isinstance(individual, dict):
        remaining = _percentage(individual.get("remainingPercent"))
        reset_at = _nonnegative_int(individual.get("resetsAt"))
        limit = _bounded_text(individual.get("limit"), limit=64)
        used = _bounded_text(individual.get("used"), limit=64)
        if remaining is not None and reset_at is not None and limit and used:
            normalized_individual = {
                "limit": limit,
                "used": used,
                "remaining_percent": remaining,
                "resets_at": reset_at,
            }
    return {
        "limit_id": _bounded_text(value.get("limitId"), limit=96) or fallback_id[:96],
        "limit_name": _bounded_text(value.get("limitName"), limit=120),
        "plan_type": _bounded_text(value.get("planType"), limit=64),
        "primary": primary,
        "secondary": secondary,
        "credits": normalized_credits,
        "individual_limit": normalized_individual,
        "spend_control_reached": (
            value.get("spendControlReached")
            if isinstance(value.get("spendControlReached"), bool)
            else None
        ),
        "rate_limit_reached_type": _bounded_text(
            value.get("rateLimitReachedType"),
            limit=96,
        ),
    }


@dataclass
class _ActiveDeviceLogin:
    owner: tuple[str, str]
    client: CodexAppServer
    task: asyncio.Task[None] | None = None


_DEVICE_LOGINS: dict[str, _ActiveDeviceLogin] = {}
_DEVICE_LOGIN_BY_OWNER: dict[tuple[str, str], str] = {}
_DEVICE_LOGIN_LOCK = asyncio.Lock()
_DEVICE_LOGIN_TIMEOUT_S = 15 * 60

# ``list_models`` opens its own app-server against the account's shared auth
# home. A concurrent Chat Turn can make that probe fail even though the
# credential remains configured and the Turn is successfully using it. Keep
# the last successful catalog for as long as the managed credential still
# exists. A time-based expiry is incorrect here: long-running Turns routinely
# outlive it and would make the UI report a false disconnect mid-response.
# Explicit logout (and a missing credential file) invalidates the cache.
_MODEL_LIST_CACHE: dict[tuple[str, str], list[RuntimeModelOption]] = {}


class CodexAccountService:
    """Use only the official Codex CLI/app-server account operations."""

    def __init__(self, tenant_id: str, user_id: str) -> None:
        self._tenant = _safe_component(str(tenant_id))
        self._user = _safe_component(str(user_id))
        self._home = codex_account_auth_home(self._tenant, self._user)

    @property
    def _owner(self) -> tuple[str, str]:
        return (self._tenant, self._user)

    def _prepare_home(self) -> None:
        os.makedirs(self._home, mode=0o700, exist_ok=True)
        os.chmod(self._home, 0o700)

    @staticmethod
    def _executable() -> str | None:
        return resolve_codex_executable()

    def _env(self) -> dict[str, str]:
        allowed = {
            "PATH",
            "LANG",
            "LC_ALL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "CODEX_CA_CERTIFICATE",
        }
        environment = {
            key: value for key, value in os.environ.items() if key in allowed
        }
        environment["CODEX_HOME"] = self._home
        environment["CODEX_SQLITE_HOME"] = self._home
        return environment

    async def _run(self, *arguments: str, timeout_s: float = 60.0) -> int:
        executable = self._executable()
        if not executable:
            raise RuntimeError("codex_cli_unavailable")
        self._prepare_home()
        process = await asyncio.create_subprocess_exec(
            executable,
            "-c",
            _CODEX_FILE_CREDENTIAL_CONFIG,
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env(),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError("codex_auth_command_timed_out") from exc
        return int(process.returncode or 0)

    async def status(self) -> CodexAccountStatus:
        available = self._executable() is not None
        if not available:
            return CodexAccountStatus(cli_available=False, authenticated=False)
        # This endpoint reports whether Flowork has a persistent Codex
        # credential configured, not whether a second CLI process can acquire
        # the same auth home at this instant. The official login command owns
        # creation/removal of auth.json; probing it again with ``login status``
        # races an active app-server and caused the Settings page and composer
        # to flicker to "disconnected" during otherwise healthy Turns.
        authenticated = os.path.isfile(os.path.join(self._home, "auth.json"))
        if not authenticated:
            _MODEL_LIST_CACHE.pop(self._owner, None)
        return CodexAccountStatus(
            cli_available=True,
            authenticated=authenticated,
        )

    async def start_device_login(self) -> CodexDeviceLogin:
        executable = self._executable()
        if not executable:
            raise RuntimeError("codex_cli_unavailable")
        self._prepare_home()
        await self._cancel_device_login_for_owner()
        client = CodexAppServer(
            executable=executable,
            env=self._env(),
            cwd=self._home,
        )
        try:
            await client.start()
            result = await client.request(
                "account/login/start",
                {"type": "chatgptDeviceCode"},
                timeout_s=30,
            )
            if result.get("type") != "chatgptDeviceCode":
                raise RuntimeError("codex_device_login_invalid_response")
            login_id = str(result.get("loginId") or "")
            verification_url = str(result.get("verificationUrl") or "")
            user_code = str(result.get("userCode") or "")
            if (
                not login_id
                or not verification_url.startswith("https://")
                or not user_code
                or len(user_code) > 128
            ):
                raise RuntimeError("codex_device_login_invalid_response")
        except CodexAppServerError as exc:
            await client.close()
            raise RuntimeError(exc.code) from exc
        except Exception:
            await client.close()
            raise

        active = _ActiveDeviceLogin(owner=self._owner, client=client)
        async with _DEVICE_LOGIN_LOCK:
            _DEVICE_LOGINS[login_id] = active
            _DEVICE_LOGIN_BY_OWNER[self._owner] = login_id
        active.task = asyncio.create_task(self._watch_device_login(login_id, active))
        expires_at = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + _DEVICE_LOGIN_TIMEOUT_S,
            tz=timezone.utc,
        ).isoformat()
        return CodexDeviceLogin(
            login_session_id=login_id,
            verification_url=verification_url,
            user_code=user_code,
            expires_at=expires_at,
        )

    async def _watch_device_login(
        self,
        login_id: str,
        active: _ActiveDeviceLogin,
    ) -> None:
        try:
            async with asyncio.timeout(_DEVICE_LOGIN_TIMEOUT_S):
                async for message in active.client.messages():
                    if message.get("method") != "account/login/completed":
                        continue
                    params = message.get("params")
                    if not isinstance(params, dict):
                        continue
                    if params.get("loginId") in {None, login_id}:
                        return
        except TimeoutError:
            try:
                await active.client.request(
                    "account/login/cancel",
                    {"loginId": login_id},
                    timeout_s=5,
                )
            except Exception:
                pass
        finally:
            await active.client.close()
            async with _DEVICE_LOGIN_LOCK:
                if _DEVICE_LOGINS.get(login_id) is active:
                    _DEVICE_LOGINS.pop(login_id, None)
                if _DEVICE_LOGIN_BY_OWNER.get(active.owner) == login_id:
                    _DEVICE_LOGIN_BY_OWNER.pop(active.owner, None)

    async def _cancel_device_login_for_owner(self) -> None:
        async with _DEVICE_LOGIN_LOCK:
            login_id = _DEVICE_LOGIN_BY_OWNER.pop(self._owner, None)
            active = _DEVICE_LOGINS.pop(login_id, None) if login_id else None
        if active is None:
            return
        try:
            await active.client.request(
                "account/login/cancel",
                {"loginId": login_id},
                timeout_s=5,
            )
        except Exception:
            pass
        await active.client.close()
        if active.task is not None and active.task is not asyncio.current_task():
            active.task.cancel()
            await asyncio.gather(active.task, return_exceptions=True)

    async def logout(self) -> CodexAccountStatus:
        await self._cancel_device_login_for_owner()
        if await self._run("logout", timeout_s=30) != 0:
            raise RuntimeError("codex_logout_failed")
        _MODEL_LIST_CACHE.pop(self._owner, None)
        return await self.status()

    async def usage_snapshot(self, *, timeout_s: float = 30) -> dict[str, Any]:
        """Return a bounded, browser-safe projection of Codex account usage.

        The official app-server owns token refresh and the upstream schema. We
        never parse terminal output or expose its raw account/rate-limit
        payloads to the browser.
        """
        executable = self._executable()
        if not executable:
            raise RuntimeError("codex_cli_unavailable")
        self._prepare_home()
        client = CodexAppServer(executable=executable, env=self._env(), cwd=self._home)
        try:
            await client.start()
            account_result = await client.request(
                "account/read",
                {"refreshToken": False},
                timeout_s=timeout_s,
            )
            account = account_result.get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise RuntimeError("codex_account_not_authenticated")

            async def optional_request(method: str) -> dict[str, Any] | None:
                try:
                    return await client.request(method, timeout_s=timeout_s)
                except CodexAppServerError:
                    return None

            rate_result, usage_result = await asyncio.gather(
                optional_request("account/rateLimits/read"),
                optional_request("account/usage/read"),
            )
        except CodexAppServerError as exc:
            raise RuntimeError(exc.code) from exc
        finally:
            await client.close()

        unavailable_sections: list[str] = []
        rate_limits: list[dict[str, Any]] = []
        reset_credits_available: int | None = None
        if rate_result is None:
            unavailable_sections.append("rate_limits")
        else:
            raw_buckets = rate_result.get("rateLimitsByLimitId")
            candidates: list[tuple[str, Any]] = []
            if isinstance(raw_buckets, dict) and raw_buckets:
                candidates = list(raw_buckets.items())[:32]
            else:
                candidates = [("codex", rate_result.get("rateLimits"))]
            for fallback_id, raw_bucket in candidates:
                normalized = _normalize_rate_bucket(
                    raw_bucket,
                    fallback_id=str(fallback_id),
                )
                if normalized is not None:
                    rate_limits.append(normalized)
            reset_credits = rate_result.get("rateLimitResetCredits")
            if isinstance(reset_credits, dict):
                reset_credits_available = _nonnegative_int(
                    reset_credits.get("availableCount")
                )

        summary: dict[str, int | None] | None = None
        daily_usage: list[dict[str, Any]] = []
        if usage_result is None:
            unavailable_sections.append("activity")
        else:
            raw_summary = usage_result.get("summary")
            if isinstance(raw_summary, dict):
                summary = {
                    "lifetime_tokens": _nonnegative_int(
                        raw_summary.get("lifetimeTokens")
                    ),
                    "peak_daily_tokens": _nonnegative_int(
                        raw_summary.get("peakDailyTokens")
                    ),
                    "longest_running_turn_sec": _nonnegative_int(
                        raw_summary.get("longestRunningTurnSec")
                    ),
                    "current_streak_days": _nonnegative_int(
                        raw_summary.get("currentStreakDays")
                    ),
                    "longest_streak_days": _nonnegative_int(
                        raw_summary.get("longestStreakDays")
                    ),
                }
            buckets = usage_result.get("dailyUsageBuckets")
            if isinstance(buckets, list):
                for bucket in buckets[-400:]:
                    if not isinstance(bucket, dict):
                        continue
                    start_date = _bounded_text(bucket.get("startDate"), limit=10)
                    tokens = _nonnegative_int(bucket.get("tokens"))
                    if start_date is None or tokens is None:
                        continue
                    try:
                        date.fromisoformat(start_date)
                    except ValueError:
                        continue
                    daily_usage.append({"start_date": start_date, "tokens": tokens})
                daily_usage.sort(key=lambda item: item["start_date"])

        return {
            "email": _bounded_text(account.get("email"), limit=320),
            "plan_type": _bounded_text(account.get("planType"), limit=64),
            "rate_limits": rate_limits,
            "rate_limit_reset_credits_available": reset_credits_available,
            "usage_summary": summary,
            "daily_usage_buckets": daily_usage,
            "unavailable_sections": unavailable_sections,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    async def list_models(self, *, timeout_s: float = 30) -> list[RuntimeModelOption]:
        executable = self._executable()
        if not executable:
            raise RuntimeError("codex_cli_unavailable")
        self._prepare_home()
        client = CodexAppServer(executable=executable, env=self._env(), cwd=self._home)
        try:
            await client.start()
            raw_models: list[dict[str, Any]] = []
            cursor: str | None = None
            while True:
                result = await client.request(
                    "model/list",
                    {
                        "cursor": cursor,
                        "limit": 100,
                        "includeHidden": False,
                    },
                    timeout_s=timeout_s,
                )
                page = result.get("data")
                if not isinstance(page, list):
                    raise RuntimeError("codex_model_catalog_invalid_response")
                raw_models.extend(item for item in page if isinstance(item, dict))
                next_cursor = result.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
        except CodexAppServerError as exc:
            cached = _MODEL_LIST_CACHE.get(self._owner)
            if cached is not None and os.path.isfile(
                os.path.join(self._home, "auth.json")
            ):
                # The credential is still configured. The probe's app-server
                # is most likely contending with an in-progress Turn, so keep
                # the last verified catalog regardless of Turn duration.
                return cached
            raise RuntimeError(exc.code) from exc
        finally:
            await client.close()
        models: list[RuntimeModelOption] = []
        for raw in raw_models:
            model_id = raw.get("model") or raw.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            efforts: list[RuntimeReasoningEffortOption] = []
            advertised = raw.get("supportedReasoningEfforts")
            if isinstance(advertised, list):
                for option in advertised:
                    if not isinstance(option, dict):
                        continue
                    effort = option.get("reasoningEffort")
                    if isinstance(effort, str) and effort:
                        efforts.append(
                            RuntimeReasoningEffortOption(
                                id=effort,
                                label=effort,
                                description=str(option.get("description") or ""),
                            )
                        )
            default_effort = raw.get("defaultReasoningEffort")
            models.append(
                RuntimeModelOption(
                    id=model_id,
                    label=str(raw.get("displayName") or model_id),
                    description=str(raw.get("description") or ""),
                    provider="chatgpt",
                    is_default=bool(raw.get("isDefault")),
                    supported_reasoning_efforts=efforts,
                    default_reasoning_effort=(
                        default_effort
                        if isinstance(default_effort, str) and default_effort
                        else None
                    ),
                )
            )
        _MODEL_LIST_CACHE[self._owner] = models
        return models


__all__ = [
    "CodexAccountService",
    "CodexAccountStatus",
    "CodexDeviceLogin",
    "codex_account_auth_file",
    "codex_account_auth_home",
]
