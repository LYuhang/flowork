"""Agent Runtime settings and user-scoped runtime authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.config import config
from vibecanvas_api.routes.deps import get_agent_runtime_repo
from vibecanvas_api.schemas.chat import (
    AgentRuntimeSettingsOut,
    AgentRuntimeSettingsUpdate,
    CodexManagedProfileUpdate,
    UserTimezoneUpdate,
)
from vibecanvas_api.services.agent_runtime.capabilities import (
    codex_capabilities,
    runtime_model_connection_id,
)
from vibecanvas_api.services.agent_runtime.codex_account import CodexAccountService
from vibecanvas_api.services.agent_runtime.protocol import RuntimeCapabilities
from vibecanvas_api.services.agent_runtime.registry import (
    AVAILABLE_RUNTIME_TYPES,
    runtime_options,
)
from vibecanvas_api.services.sandbox.manager import get_existing_sandbox_manager
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo

router = APIRouter(prefix="/api/v1/agent-runtime", tags=["agent-runtime"])


class CodexAccountStatusOut(BaseModel):
    cli_available: bool
    authenticated: bool


class CodexRateLimitWindowOut(BaseModel):
    used_percent: float
    window_duration_mins: int | None = None
    resets_at: int | None = None


class CodexCreditsOut(BaseModel):
    has_credits: bool
    unlimited: bool
    balance: str | None = None


class CodexIndividualLimitOut(BaseModel):
    limit: str
    used: str
    remaining_percent: float
    resets_at: int


class CodexRateLimitBucketOut(BaseModel):
    limit_id: str
    limit_name: str | None = None
    plan_type: str | None = None
    primary: CodexRateLimitWindowOut | None = None
    secondary: CodexRateLimitWindowOut | None = None
    credits: CodexCreditsOut | None = None
    individual_limit: CodexIndividualLimitOut | None = None
    spend_control_reached: bool | None = None
    rate_limit_reached_type: str | None = None


class CodexUsageSummaryOut(BaseModel):
    lifetime_tokens: int | None = None
    peak_daily_tokens: int | None = None
    longest_running_turn_sec: int | None = None
    current_streak_days: int | None = None
    longest_streak_days: int | None = None


class CodexDailyUsageOut(BaseModel):
    start_date: str
    tokens: int


class CodexAccountUsageOut(BaseModel):
    email: str | None = None
    plan_type: str | None = None
    rate_limits: list[CodexRateLimitBucketOut]
    rate_limit_reset_credits_available: int | None = None
    usage_summary: CodexUsageSummaryOut | None = None
    daily_usage_buckets: list[CodexDailyUsageOut]
    unavailable_sections: list[str]
    fetched_at: str


class CodexDeviceLoginOut(BaseModel):
    login_session_id: str
    verification_url: str
    user_code: str
    expires_at: str


def _with_chat_model_default(
    capabilities: RuntimeCapabilities,
    binding: dict | None,
) -> RuntimeCapabilities:
    """Render one Chat's connection-locked model catalog.

    A new Chat may choose any connection compatible with its Runtime. The first
    accepted Turn fixes the exact non-secret connection id; later capability
    reads expose only models from that connection. This keeps provider-native
    history, credentials, billing, and audit identity stable while still
    allowing model and reasoning changes within the connection.
    """
    connection_id = (
        binding.get("runtime_connection_id") if binding is not None else None
    )
    models = capabilities.models
    if connection_id:
        models = [
            model
            for model in models
            if runtime_model_connection_id(capabilities.runtime_type, model.id)
            == connection_id
        ]
    capabilities = capabilities.model_copy(update={
        "models": models,
        "bound_agent_settings": (
            binding.get("runtime_agent_settings") if binding is not None else None
        ),
    })
    model_id = binding.get("runtime_model_id") if binding is not None else None
    if not model_id:
        return capabilities
    if not any(model.id == model_id for model in models):
        return capabilities.model_copy(
            update={
                "default_model_id": None,
                "error_code": "runtime_model_unavailable",
            }
        )
    return capabilities.model_copy(
        update={
            "default_model_id": model_id,
            "models": [
                model.model_copy(update={"is_default": model.id == model_id})
                for model in models
            ],
        }
    )


def _codex_account_service(auth: AuthContext) -> CodexAccountService:
    return CodexAccountService(auth.tenant_id, auth.user_id)


def _codex_method_enabled(method: str) -> bool:
    return (
        "codex" in config.agent_runtime_types
        and method in config.codex_runtime_auth_methods
    )


def _settings_out(preferences: dict) -> AgentRuntimeSettingsOut:
    allowed = {str(profile["id"]) for profile in config.codex_managed_apis}
    selected = preferences.get("codex_managed_profile_id")
    return AgentRuntimeSettingsOut(
        **{
            **preferences,
            "codex_managed_profile_id": selected if selected in allowed else None,
        },
        available_runtime_types=[
            runtime
            for runtime in config.agent_runtime_types
            if runtime in AVAILABLE_RUNTIME_TYPES
        ],
        runtime_options=runtime_options(config.agent_runtime_types),
        codex_auth_methods=(
            list(config.codex_runtime_auth_methods)
            if "codex" in config.agent_runtime_types
            else []
        ),
        codex_managed_profiles=[
            {
                "id": str(profile["id"]),
                "name": str(profile["name"]),
                "model_count": len(profile["models"]),
            }
            for profile in (
                config.codex_managed_apis
                if _codex_method_enabled("managed_api")
                else ()
            )
        ],
    )


@router.get("/capabilities", response_model=RuntimeCapabilities)
async def get_agent_runtime_capabilities(
    chat_id: str | None = Query(default=None),
    runtime_repo=Depends(get_agent_runtime_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
) -> RuntimeCapabilities:
    """Return the catalog for the Chat's immutable runtime binding.

    Draft Chats are intentionally not created by this read. Until the first
    Turn atomically binds them, they preview the user's current default runtime.
    """
    runtime_type: str | None = None
    binding: dict | None = None
    if chat_id:
        binding = await runtime_repo.get_chat_binding(chat_id)
        if binding is not None:
            runtime_type = binding.get("runtime_type")
    if runtime_type is None:
        preferences = await runtime_repo.get_preferences()
        runtime_type = preferences["default_runtime_type"]
    else:
        preferences = await runtime_repo.get_preferences()

    if runtime_type == "codex":
        credentials = await LlmCredentialsRepo(session).list_for_user(auth.user_id)
        selected_profile = preferences.get("codex_managed_profile_id")
        if selected_profile not in {
            str(profile["id"]) for profile in config.codex_managed_apis
        }:
            selected_profile = None
        return _with_chat_model_default(
            await codex_capabilities(
                credentials,
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                selected_managed_profile_id=selected_profile,
                auth_methods=config.codex_runtime_auth_methods,
            ),
            binding,
        )
    raise HTTPException(
        status_code=409,
        detail={"code": "runtime_adapter_unavailable", "runtime_type": runtime_type},
    )


@router.get("/settings", response_model=AgentRuntimeSettingsOut)
async def get_agent_runtime_settings(
    runtime_repo=Depends(get_agent_runtime_repo),
) -> AgentRuntimeSettingsOut:
    prefs = await runtime_repo.get_preferences()
    return _settings_out(prefs)


@router.get("/codex/account", response_model=CodexAccountStatusOut)
async def get_codex_account_status(
    auth: AuthContext = Depends(current_user),
) -> CodexAccountStatusOut:
    if not _codex_method_enabled("chatgpt"):
        raise HTTPException(
            status_code=404,
            detail={"code": "codex_auth_method_disabled"},
        )
    status = await _codex_account_service(auth).status()
    return CodexAccountStatusOut(**status.__dict__)


@router.get("/codex/account/usage", response_model=CodexAccountUsageOut)
async def get_codex_account_usage(
    auth: AuthContext = Depends(current_user),
) -> CodexAccountUsageOut:
    if not _codex_method_enabled("chatgpt"):
        raise HTTPException(
            status_code=404,
            detail={"code": "codex_auth_method_disabled"},
        )
    try:
        snapshot = await _codex_account_service(auth).usage_snapshot()
    except RuntimeError as exc:
        code = str(exc)
        status_code = (
            409
            if code == "codex_account_not_authenticated"
            else 503
            if code == "codex_cli_unavailable"
            else 502
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": code},
        ) from exc
    return CodexAccountUsageOut(**snapshot)


@router.post("/codex/account/device", response_model=CodexDeviceLoginOut)
async def start_codex_account_device_login(
    auth: AuthContext = Depends(current_user),
) -> CodexDeviceLoginOut:
    if not _codex_method_enabled("chatgpt"):
        raise HTTPException(
            status_code=404,
            detail={"code": "codex_auth_method_disabled"},
        )
    try:
        login = await _codex_account_service(auth).start_device_login()
    except RuntimeError as exc:
        code = str(exc)
        raise HTTPException(
            status_code=503 if code == "codex_cli_unavailable" else 502,
            detail={"code": code},
        ) from exc
    return CodexDeviceLoginOut(**login.__dict__)


@router.delete("/codex/account", response_model=CodexAccountStatusOut)
async def disconnect_codex_account(
    auth: AuthContext = Depends(current_user),
) -> CodexAccountStatusOut:
    if not _codex_method_enabled("chatgpt"):
        raise HTTPException(
            status_code=404,
            detail={"code": "codex_auth_method_disabled"},
        )
    try:
        status = await _codex_account_service(auth).logout()
    except RuntimeError as exc:
        code = str(exc)
        raise HTTPException(
            status_code=503 if code == "codex_cli_unavailable" else 502,
            detail={"code": code},
        ) from exc
    sandbox_manager = get_existing_sandbox_manager()
    if sandbox_manager is not None:
        await sandbox_manager.invalidate_codex_account_sessions(
            auth.tenant_id,
            auth.user_id,
        )
    return CodexAccountStatusOut(**status.__dict__)


@router.put("/settings", response_model=AgentRuntimeSettingsOut)
async def update_agent_runtime_settings(
    body: AgentRuntimeSettingsUpdate,
    runtime_repo=Depends(get_agent_runtime_repo),
) -> AgentRuntimeSettingsOut:
    if (
        body.default_runtime_type not in AVAILABLE_RUNTIME_TYPES
        or body.default_runtime_type not in config.agent_runtime_types
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "runtime_adapter_unavailable",
                "runtime_type": body.default_runtime_type,
            },
        )
    prefs = await runtime_repo.set_default_runtime_type(body.default_runtime_type)
    return _settings_out(prefs)


@router.put("/settings/timezone", response_model=AgentRuntimeSettingsOut)
async def update_user_timezone(
    body: UserTimezoneUpdate,
    runtime_repo=Depends(get_agent_runtime_repo),
) -> AgentRuntimeSettingsOut:
    """Persist the account timezone used by new conversations."""
    return _settings_out(
        await runtime_repo.set_preferred_timezone(body.preferred_timezone)
    )


@router.put("/codex/managed-profile", response_model=AgentRuntimeSettingsOut)
async def select_codex_managed_profile(
    body: CodexManagedProfileUpdate,
    runtime_repo=Depends(get_agent_runtime_repo),
) -> AgentRuntimeSettingsOut:
    if not _codex_method_enabled("managed_api"):
        raise HTTPException(
            status_code=404,
            detail={"code": "codex_auth_method_disabled"},
        )
    allowed = {str(profile["id"]) for profile in config.codex_managed_apis}
    if body.profile_id not in allowed:
        raise HTTPException(
            status_code=404,
            detail={"code": "codex_managed_profile_not_found"},
        )
    return _settings_out(await runtime_repo.set_codex_managed_profile(body.profile_id))
