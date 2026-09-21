from __future__ import annotations

import json

import pytest
from vibecanvas_api.config import AppConfig
from vibecanvas_api.routes import agent_runtime as agent_runtime_routes
from vibecanvas_api.routes.runtime_model_broker import _resolve_model_material
from vibecanvas_api.services.agent_runtime import capabilities as capabilities_module
from vibecanvas_api.services.agent_runtime import codex_account as codex_account_module
from vibecanvas_api.services.agent_runtime.capabilities import (
    codex_capabilities,
    codex_managed_model,
)
from vibecanvas_api.services.agent_runtime.codex import _uses_chatgpt_account
from vibecanvas_api.services.agent_runtime.codex_account import CodexAccountService
from vibecanvas_api.services.agent_runtime.model_capability import (
    mint_runtime_model_capability,
    model_config_revision,
    verify_runtime_model_capability,
)
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeCapabilities,
    RuntimeModelOption,
    RuntimeTurnRequest,
    RuntimeType,
)


def test_deployment_runtime_and_codex_auth_choices_are_validated(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_TYPES", "codex")
    monkeypatch.setenv(
        "CODEX_RUNTIME_AUTH_METHODS",
        "chatgpt,managed_api",
    )
    monkeypatch.setenv(
        "CODEX_MANAGED_APIS_JSON",
        json.dumps(
            [
                {
                    "name": "Corporate OpenAI",
                    "base_url": "https://openai.example.test/v1",
                    "api_key": "server-only-key",
                    "models": ["gpt-codex-company", "gpt-codex-fast"],
                }
            ]
        ),
    )

    configured = AppConfig({})

    assert configured.agent_runtime_types == ("codex",)
    assert configured.codex_runtime_auth_methods == ("chatgpt", "managed_api")
    assert configured.codex_managed_apis[0]["name"] == "Corporate OpenAI"
    assert configured.codex_managed_apis[0]["models"] == (
        "gpt-codex-company",
        "gpt-codex-fast",
    )


def test_invalid_runtime_choice_fails_startup(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_TYPES", "codex,unknown")
    with pytest.raises(ValueError, match="AGENT_RUNTIME_TYPES"):
        AppConfig({})


def test_bound_chat_model_replaces_only_the_catalog_default():
    capabilities = RuntimeCapabilities(
        runtime_type=RuntimeType.CODEX,
        runtime_available=True,
        authenticated=True,
        source="test",
        models=[
            RuntimeModelOption(
                id="codex:managed:corp:gpt-managed",
                label="Managed",
                is_default=True,
            ),
            RuntimeModelOption(id="codex:account:gpt", label="Account"),
            RuntimeModelOption(id="codex:account:gpt-alt", label="Account alt"),
        ],
        default_model_id="codex:managed:corp:gpt-managed",
    )

    rebound = agent_runtime_routes._with_chat_model_default(
        capabilities,
        {
            "runtime_model_id": "codex:account:gpt",
            "runtime_connection_id": "codex:account",
        },
    )

    assert rebound.default_model_id == "codex:account:gpt"
    assert [model.id for model in rebound.models] == [
        "codex:account:gpt",
        "codex:account:gpt-alt",
    ]
    assert [model.is_default for model in rebound.models] == [True, False]
    # The helper returns copies; a per-Chat projection cannot mutate the
    # process-shared catalog used by another Chat.
    assert capabilities.default_model_id == "codex:managed:corp:gpt-managed"
    assert [model.is_default for model in capabilities.models] == [
        True,
        False,
        False,
    ]


def test_bound_chat_does_not_fall_back_to_another_api_when_its_model_is_missing():
    capabilities = RuntimeCapabilities(
        runtime_type=RuntimeType.CODEX,
        runtime_available=True,
        authenticated=True,
        source="test",
        models=[
            RuntimeModelOption(
                id="codex:credential:22222222-2222-4222-8222-222222222222",
                label="Another API",
                is_default=True,
            ),
        ],
        default_model_id=(
            "codex:credential:22222222-2222-4222-8222-222222222222"
        ),
    )

    rebound = agent_runtime_routes._with_chat_model_default(
        capabilities,
        {
            "runtime_model_id": (
                "codex:credential:11111111-1111-4111-8111-111111111111"
            ),
            "runtime_connection_id": (
                "codex:credential:11111111-1111-4111-8111-111111111111"
            ),
        },
    )

    assert rebound.authenticated is True
    assert rebound.models == []
    assert rebound.default_model_id is None
    assert rebound.error_code == "runtime_model_unavailable"


def test_codex_account_process_environment_excludes_model_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("AGENT_API_KEY", "must-not-leak-either")
    service = CodexAccountService("tenant", "user")

    environment = service._env()

    assert "OPENAI_API_KEY" not in environment
    assert "AGENT_API_KEY" not in environment
    assert environment["CODEX_HOME"].endswith("/tenant/user/codex-account-v1/.codex")


@pytest.mark.asyncio
async def test_codex_account_usage_uses_app_server_and_bounds_public_fields(
    monkeypatch,
    tmp_path,
):
    calls: list[str] = []

    class FakeAppServer:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            calls.append("start")

        async def close(self):
            calls.append("close")

        async def request(self, method, _params=None, *, timeout_s=30):
            assert timeout_s == 30
            calls.append(method)
            if method == "account/read":
                return {
                    "account": {
                        "type": "chatgpt",
                        "email": "user@example.com",
                        "planType": "pro",
                    },
                }
            if method == "account/rateLimits/read":
                return {
                    "rateLimits": {},
                    "rateLimitsByLimitId": {
                        "codex": {
                            "limitId": "codex",
                            "limitName": None,
                            "primary": {
                                "usedPercent": 125,
                                "windowDurationMins": 300,
                                "resetsAt": 1_780_000_000,
                            },
                            "secondary": None,
                            "credits": {
                                "hasCredits": True,
                                "unlimited": False,
                                "balance": "42.5",
                            },
                            "individualLimit": None,
                            "spendControlReached": False,
                            "planType": "pro",
                            "rateLimitReachedType": None,
                        },
                    },
                    "rateLimitResetCredits": {"availableCount": 2},
                }
            if method == "account/usage/read":
                return {
                    "summary": {
                        "lifetimeTokens": 123_456,
                        "peakDailyTokens": 20_000,
                        "longestRunningTurnSec": 90,
                        "currentStreakDays": 4,
                        "longestStreakDays": 8,
                    },
                    "dailyUsageBuckets": [
                        {"startDate": "not-a-date", "tokens": 999},
                        {"startDate": "2026-08-06", "tokens": 1000},
                    ],
                }
            raise AssertionError(method)

    monkeypatch.setattr(codex_account_module, "CodexAppServer", FakeAppServer)
    monkeypatch.setattr(
        CodexAccountService,
        "_executable",
        staticmethod(lambda: "/opt/codex/bin/codex"),
    )
    service = CodexAccountService("tenant", "user")
    service._home = str(tmp_path / ".codex")

    snapshot = await service.usage_snapshot()

    assert snapshot["email"] == "user@example.com"
    assert snapshot["plan_type"] == "pro"
    assert snapshot["rate_limits"][0]["primary"]["used_percent"] == 100.0
    assert snapshot["rate_limits"][0]["credits"]["balance"] == "42.5"
    assert snapshot["rate_limit_reset_credits_available"] == 2
    assert snapshot["usage_summary"]["lifetime_tokens"] == 123_456
    assert snapshot["daily_usage_buckets"] == [
        {"start_date": "2026-08-06", "tokens": 1000}
    ]
    assert snapshot["unavailable_sections"] == []
    assert calls == [
        "start",
        "account/read",
        "account/rateLimits/read",
        "account/usage/read",
        "close",
    ]


@pytest.mark.asyncio
async def test_managed_codex_profiles_expose_only_name_and_model(monkeypatch):
    monkeypatch.setattr(
        capabilities_module,
        "resolve_codex_executable",
        lambda: "/opt/codex/bin/codex",
    )
    monkeypatch.setattr(capabilities_module.config.agent, "model", "openai:platform")
    monkeypatch.setattr(
        capabilities_module.config,
        "codex_managed_apis",
        [
            {
                "id": "corp-primary",
                "name": "Corporate OpenAI",
                "base_url": "https://secret.example.test/v1",
                "api_key": "must-not-leak",
                "models": ("gpt-codex-company",),
            }
        ],
    )

    capabilities = await codex_capabilities(
        [],
        selected_managed_profile_id="corp-primary",
        auth_methods=["managed_api"],
    )

    selected = next(model for model in capabilities.models if model.is_default)
    assert selected.label == "Corporate OpenAI · gpt-codex-company"
    assert "secret.internal" not in selected.model_dump_json()
    assert "must-not-leak" not in selected.model_dump_json()
    assert codex_managed_model(selected.id) == (
        "corp-primary",
        "gpt-codex-company",
    )


def test_managed_profile_is_bound_into_signed_runtime_capability():
    token = mint_runtime_model_capability(
        organization_id="org",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_session_id="runtime",
        session_id="00000000-0000-0000-0000-000000000001",
        session_generation=1,
        membership_id="membership",
        credential_id=None,
        managed_profile_id="corp-primary",
        provider="openai",
        model="gpt-codex-company",
        config_revision="revision",
        authorization_generation="generation",
        resources=["chat:chat"],
        actions=["chat:execute", "model:invoke"],
        secret="test-secret",
        ttl_s=60,
        now=100,
    )
    verified = verify_runtime_model_capability(
        token,
        secret="test-secret",
        now=101,
    )
    assert verified is not None
    assert verified.managed_profile_id == "corp-primary"


@pytest.mark.asyncio
async def test_managed_profile_secret_is_resolved_only_on_host(monkeypatch):
    profile = {
        "id": "corp-primary",
        "name": "Corporate OpenAI",
        "base_url": "https://openai.example.test/v1",
        "api_key": "server-only-key",
        "models": ("gpt-codex-company",),
    }
    monkeypatch.setattr(
        agent_runtime_routes.config,
        "codex_managed_apis",
        [profile],
    )
    monkeypatch.setattr(agent_runtime_routes.config, "environment", "production")
    revision = model_config_revision(
        provider="openai",
        model="gpt-codex-company",
        updated_at="managed:corp-primary",
    )
    token = mint_runtime_model_capability(
        organization_id="org",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_session_id="runtime",
        session_id="00000000-0000-0000-0000-000000000001",
        session_generation=1,
        membership_id="membership",
        credential_id=None,
        managed_profile_id="corp-primary",
        provider="openai",
        model="gpt-codex-company",
        config_revision=revision,
        authorization_generation="generation",
        resources=["chat:chat"],
        actions=["chat:execute", "model:invoke"],
        secret="test-secret",
        ttl_s=60,
        now=100,
    )
    capability = verify_runtime_model_capability(
        token,
        secret="test-secret",
        now=101,
    )
    assert capability is not None

    target = await _resolve_model_material(
        session=None,
        service=None,
        principal=None,
        authz_context=None,
        capability=capability,
    )

    assert target.base_url == "https://openai.example.test/v1"
    assert target.api_key == "server-only-key"
    assert target.pinned_addresses == {}


def test_codex_account_mode_is_explicit_in_runtime_request():
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={"id": "gpt-codex", "connection_type": "chatgpt_account"},
    )
    assert _uses_chatgpt_account(request) is True
