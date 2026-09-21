"""OpenRouter PKCE lifecycle against the real tenant/RLS database."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.routes import llm_credentials as routes
from vibecanvas_api.services import openrouter_connection


async def _register(client) -> tuple[dict[str, str], dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"openrouter_{uuid.uuid4().hex[:12]}@example.com",
            "username": "openrouter_owner",
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['session_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return headers, me


def _catalog() -> list[dict]:
    return [
        {
            "id": "openai/gpt-5",
            "name": "GPT-5",
            "description": "Primary model",
            "context_length": 400_000,
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "supports_tools": True,
            "pricing": {"prompt": "0.000001", "completion": "0.000004"},
            "available": True,
        },
        {
            "id": "anthropic/claude-sonnet",
            "name": "Claude Sonnet",
            "description": "Alternative model",
            "context_length": 200_000,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supports_tools": True,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "available": True,
        },
    ]


@pytest.mark.asyncio
async def test_openrouter_start_is_safely_disabled_without_public_url(
    client,
    monkeypatch,
) -> None:
    headers, _me = await _register(client)
    monkeypatch.setattr(
        openrouter_connection.config.public_urls,
        "public_url",
        "",
    )
    response = await client.post(
        "/api/v1/llm-credentials/openrouter/start", headers=headers,
    )
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "openrouter_public_url_required",
    }


@pytest.mark.asyncio
async def test_openrouter_connect_refresh_reconnect_and_disconnect(
    client,
    pg_engine,
    monkeypatch,
) -> None:
    headers, me = await _register(client)
    monkeypatch.setattr(
        openrouter_connection.config.public_urls,
        "public_url",
        "http://localhost:3000",
    )
    exchanged: list[tuple[str, str]] = []

    async def exchange(*, code: str, verifier: str) -> str:
        exchanged.append((code, verifier))
        return "fixture-openrouter-key"

    async def catalog(_api_key: str) -> list[dict]:
        return _catalog()

    monkeypatch.setattr(routes, "exchange_authorization_code", exchange)
    monkeypatch.setattr(routes, "fetch_user_model_catalog", catalog)

    started = await client.post(
        "/api/v1/llm-credentials/openrouter/start", headers=headers,
    )
    assert started.status_code == 200, started.text
    auth_url = urlsplit(started.json()["authorization_url"])
    assert (auth_url.scheme, auth_url.netloc, auth_url.path) == (
        "https", "openrouter.ai", "/auth",
    )
    query = parse_qs(auth_url.query)
    assert query["code_challenge_method"] == ["S256"]
    assert "code_verifier" not in query
    callback = urlsplit(query["callback_url"][0])
    assert callback.query == ""
    assert callback.path.startswith("/settings/openrouter/callback/")
    state = callback.path.rsplit("/", 1)[1]

    completed = await client.post(
        "/api/v1/llm-credentials/openrouter/callback",
        json={"code": "single-use-code", "state": state},
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    connection = completed.json()
    credential_id = connection["credential_id"]
    assert connection["connected"] is True
    assert [model["id"] for model in connection["models"]] == [
        "openai/gpt-5", "anthropic/claude-sonnet",
    ]
    assert exchanged[0][0] == "single-use-code"
    assert state not in exchanged[0][1]

    capabilities = await client.get(
        "/api/v1/agent-runtime/capabilities", headers=headers,
    )
    assert capabilities.status_code == 200, capabilities.text
    openrouter_options = [
        model for model in capabilities.json()["models"]
        if model["provider"] == "openrouter"
    ]
    assert {model["label"] for model in openrouter_options} == {
        "GPT-5", "Claude Sonnet",
    }
    assert all(model["supports_tools"] for model in openrouter_options)
    assert all(model["id"].startswith("codex:openrouter:") for model in openrouter_options)

    replay = await client.post(
        "/api/v1/llm-credentials/openrouter/callback",
        json={"code": "replayed", "state": state},
        headers=headers,
    )
    assert replay.status_code == 400
    assert replay.json()["detail"] == {"code": "openrouter_state_invalid"}

    async def transient(_api_key: str) -> list[dict]:
        raise openrouter_connection.OpenRouterConnectionError(
            "openrouter_unreachable",
        )

    monkeypatch.setattr(routes, "fetch_user_model_catalog", transient)
    failed_refresh = await client.post(
        "/api/v1/llm-credentials/openrouter/refresh", headers=headers,
    )
    assert failed_refresh.status_code == 200
    assert len(failed_refresh.json()["models"]) == 2
    assert failed_refresh.json()["error_code"] == "openrouter_unreachable"

    async def revoked(_api_key: str) -> list[dict]:
        raise openrouter_connection.OpenRouterConnectionError(
            "openrouter_credentials_rejected", upstream_status=401,
        )

    monkeypatch.setattr(routes, "fetch_user_model_catalog", revoked)
    rejected = await client.post(
        "/api/v1/llm-credentials/openrouter/refresh", headers=headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["connected"] is False
    assert len(rejected.json()["models"]) == 2

    monkeypatch.setattr(routes, "fetch_user_model_catalog", catalog)
    restarted = await client.post(
        "/api/v1/llm-credentials/openrouter/start", headers=headers,
    )
    restart_callback = urlsplit(
        parse_qs(urlsplit(restarted.json()["authorization_url"]).query)[
            "callback_url"
        ][0]
    )
    assert restart_callback.query == ""
    restart_state = restart_callback.path.rsplit("/", 1)[1]
    reconnected = await client.post(
        "/api/v1/llm-credentials/openrouter/callback",
        json={"code": "new-code", "state": restart_state},
        headers=headers,
    )
    assert reconnected.status_code == 200, reconnected.text
    assert reconnected.json()["credential_id"] == credential_id
    assert reconnected.json()["connected"] is True

    listed = await client.get("/api/v1/llm-credentials", headers=headers)
    assert "fixture-openrouter-key" not in listed.text
    assert listed.json()[0]["connection_kind"] == "openrouter_oauth"

    async with pg_engine.connect() as connection:
        state_rows = await connection.scalar(text(
            "SELECT count(*) FROM openrouter_oauth_states "
            "WHERE user_id=CAST(:user_id AS uuid)"
        ), {"user_id": me["user_id"]})
        plaintext_keys = await connection.scalar(text(
            "SELECT count(*) FROM encrypted_secrets "
            "WHERE resource_type IN ('openrouter_oauth_state','llm_credential') "
            "AND ciphertext LIKE '%fixture-openrouter-key%'"
        ))
        audits = await connection.scalar(text(
            "SELECT count(*) FROM audit_log "
            "WHERE action='llm_credential.connection_change'"
        ))
    assert state_rows == 0
    assert plaintext_keys == 0
    assert audits >= 6

    disconnected = await client.delete(
        "/api/v1/llm-credentials/openrouter", headers=headers,
    )
    assert disconnected.status_code == 204
    status = await client.get(
        "/api/v1/llm-credentials/openrouter/status", headers=headers,
    )
    assert status.json()["connected"] is False
