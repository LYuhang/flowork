"""OAuth 2.1 + PKCE account connections for remote MCP servers."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.config import config
from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.services.pinned_http import request_pinned_public_url
from vibecanvas_api.services.public_url import (
    PublicUrlError,
    validate_public_http_url,
)
from vibecanvas_api.storage.repo_mcp_servers import McpOAuthRepo, McpServersRepo

_USER_AGENT = "flowork/1.0 MCP OAuth client"
_TRANSACTION_TTL = timedelta(minutes=10)

_OAUTH_TRANSACTION_PURPOSE = "mcp_oauth_transaction"
_OAUTH_CONNECTION_PURPOSE = "mcp_oauth_tokens"


def _trusted_proxy_cidrs() -> tuple[str, ...]:
    if config.sandbox_egress_mode != "proxy":
        return ()
    return config.sandbox_egress_trusted_proxy_cidrs


def _secret_payload(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _resolve_transaction_bundle(
    session: AsyncSession, transaction: dict,
) -> dict[str, Any]:
    secret_ref = transaction.get("secret_ref")
    if not secret_ref:
        raise RuntimeError("stored OAuth transaction has no SecretService reference")
    plaintext = await secret_service().resolve_text(
        session,
        secret_ref=secret_ref,
        tenant_id=transaction["tenant_id"],
        purpose=_OAUTH_TRANSACTION_PURPOSE,
        resource_type="mcp_oauth_transaction",
        resource_id=transaction["state_hash"],
    )
    try:
        payload = json.loads(plaintext)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("stored OAuth transaction secret is invalid") from exc
    if not isinstance(payload, dict) or not payload.get("code_verifier"):
        raise RuntimeError("stored OAuth transaction secret is incomplete")
    return payload


async def _resolve_connection_bundle(
    session: AsyncSession, connection: dict,
) -> dict[str, Any]:
    secret_ref = connection.get("secret_ref")
    if not secret_ref:
        raise RuntimeError("stored OAuth connection has no SecretService reference")
    plaintext = await secret_service().resolve_text(
        session,
        secret_ref=secret_ref,
        tenant_id=connection["tenant_id"],
        purpose=_OAUTH_CONNECTION_PURPOSE,
        resource_type="mcp_installation",
        resource_id=connection["server_id"],
    )
    try:
        payload = json.loads(plaintext)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("stored OAuth connection secret is invalid") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("stored OAuth connection secret is incomplete")
    return payload


async def _destroy_secret_row(session: AsyncSession, row: dict | None) -> None:
    if row and row.get("secret_ref"):
        await secret_service().destroy(
            session,
            secret_ref=row["secret_ref"],
            tenant_id=row["tenant_id"],
        )


async def delete_oauth_transaction(
    session: AsyncSession, transaction_hash: str,
) -> None:
    deleted = await McpOAuthRepo(session).delete_transaction(transaction_hash)
    await _destroy_secret_row(session, deleted)


async def delete_oauth_transactions_for_server(
    session: AsyncSession, server_id: uuid.UUID,
) -> None:
    deleted = await McpOAuthRepo(session).delete_transactions_for_server(server_id)
    for row in deleted:
        await _destroy_secret_row(session, row)


async def delete_oauth_connection(
    session: AsyncSession, server_id: uuid.UUID,
) -> None:
    deleted = await McpOAuthRepo(session).delete_connection(server_id)
    await _destroy_secret_row(session, deleted)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("ascii")).hexdigest()


async def _validate_public_https_url(value: str, *, label: str) -> str:
    try:
        target = await validate_public_http_url(
            value,
            label=label,
            require_https=True,
            trusted_proxy_cidrs=_trusted_proxy_cidrs(),
        )
    except PublicUrlError as exc:
        raise ValueError(str(exc)) from exc
    return target.url


async def _get_json(url: str, *, label: str, timeout: httpx.Timeout) -> dict[str, Any]:
    response = await request_pinned_public_url(
        "GET",
        url,
        label=label,
        timeout=timeout,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        trusted_proxy_cidrs=_trusted_proxy_cidrs(),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError(f"{label} returned a non-object document")
    return payload


def _metadata_candidates(issuer: str) -> list[str]:
    parts = urlsplit(issuer.rstrip("/"))
    path = parts.path.rstrip("/")
    origin = f"{parts.scheme}://{parts.netloc}"
    return [
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
    ]


async def discover_authorization(
    *, endpoint: str, auth_metadata_url: str,
) -> dict[str, Any]:
    timeout = httpx.Timeout(15.0, connect=6.0)
    protected = await _get_json(
        auth_metadata_url,
        label="protected resource metadata",
        timeout=timeout,
    )
    resource = str(protected.get("resource") or endpoint)
    if resource.rstrip("/") != endpoint.rstrip("/"):
        raise ValueError("protected resource metadata does not describe this MCP endpoint")
    issuers = protected.get("authorization_servers")
    if not isinstance(issuers, list) or not issuers:
        raise ValueError("protected resource metadata has no authorization server")
    issuer = await _validate_public_https_url(str(issuers[0]), label="authorization server")
    metadata: dict[str, Any] | None = None
    last_error: Exception | None = None
    for candidate in dict.fromkeys(_metadata_candidates(issuer)):
        try:
            metadata = await _get_json(
                candidate,
                label="authorization server metadata",
                timeout=timeout,
            )
            break
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
    if metadata is None:
        raise ValueError(f"authorization server metadata could not be loaded: {last_error}")
    metadata_issuer = str(metadata.get("issuer") or "").rstrip("/")
    if metadata_issuer and metadata_issuer != issuer.rstrip("/"):
        raise ValueError("authorization server metadata issuer does not match discovery")
    challenge_methods = metadata.get("code_challenge_methods_supported") or ["S256"]
    if "S256" not in challenge_methods:
        raise ValueError("authorization server does not support PKCE S256")
    authorization_endpoint = await _validate_public_https_url(
        str(metadata.get("authorization_endpoint") or ""), label="authorization endpoint",
    )
    token_endpoint = await _validate_public_https_url(
        str(metadata.get("token_endpoint") or ""), label="token endpoint",
    )
    registration_endpoint = metadata.get("registration_endpoint")
    if registration_endpoint:
        registration_endpoint = await _validate_public_https_url(
            str(registration_endpoint), label="client registration endpoint",
        )
    supports_client_metadata = bool(
        metadata.get("client_id_metadata_document_supported")
    )
    if not registration_endpoint and not supports_client_metadata:
        raise ValueError(
            "authorization server supports neither client metadata documents nor dynamic client registration"
        )
    revocation_endpoint = metadata.get("revocation_endpoint")
    if revocation_endpoint:
        revocation_endpoint = await _validate_public_https_url(
            str(revocation_endpoint), label="revocation endpoint",
        )
    scopes = protected.get("scopes_supported") or metadata.get("scopes_supported") or []
    return {
        "resource": resource,
        "issuer": issuer,
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
        "registration_endpoint": registration_endpoint,
        "client_id_metadata_document_supported": supports_client_metadata,
        "revocation_endpoint": revocation_endpoint,
        "scopes": [str(scope) for scope in scopes if str(scope).strip()],
    }


async def begin_connection(
    session: AsyncSession,
    *,
    server: dict,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    return_origin: str,
) -> str:
    callback_url = config.public_urls.absolute("api/v1/mcp-servers/oauth/callback")
    origin_parts = urlsplit(return_origin)
    if origin_parts.scheme not in {"http", "https"} or not origin_parts.netloc:
        raise ValueError("return_origin must be an absolute HTTP(S) origin")
    clean_origin = f"{origin_parts.scheme}://{origin_parts.netloc}"

    discovered = await discover_authorization(
        endpoint=server["endpoint"],
        auth_metadata_url=server.get("auth_metadata_url") or "",
    )
    if discovered["client_id_metadata_document_supported"]:
        client_id = config.public_urls.absolute(
            "api/v1/mcp-servers/oauth/client-metadata"
        )
        client_info: dict[str, Any] = {"client_id": client_id}
    else:
        timeout = httpx.Timeout(15.0, connect=6.0)
        registration = await request_pinned_public_url(
            "POST",
            discovered["registration_endpoint"],
            label="client registration endpoint",
            timeout=timeout,
            json={
                "client_name": "Flowork",
                "redirect_uris": [callback_url],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            headers={
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            trusted_proxy_cidrs=_trusted_proxy_cidrs(),
        )
        registration.raise_for_status()
        client_info = registration.json()
        client_id = str(client_info.get("client_id") or "")
        if not client_id:
            raise ValueError("authorization server did not return a client_id")

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    scope = " ".join(discovered["scopes"])
    transaction_hash = state_hash(state)
    transaction_secret_ref = await secret_service().put_text(
        session,
        tenant_id=tenant_id,
        purpose=_OAUTH_TRANSACTION_PURPOSE,
        resource_type="mcp_oauth_transaction",
        resource_id=transaction_hash,
        plaintext=_secret_payload({
            "code_verifier": verifier,
            "client_secret": client_info.get("client_secret"),
        }),
    )
    replaced_transactions = await McpOAuthRepo(session).create_transaction(
        state_hash=transaction_hash,
        tenant_id=tenant_id,
        user_id=user_id,
        server_id=server["id"],
        secret_ref=transaction_secret_ref,
        secret_version=1,
        redirect_uri=callback_url,
        return_origin=clean_origin,
        authorization_server=discovered["issuer"],
        token_endpoint=discovered["token_endpoint"],
        revocation_endpoint=discovered["revocation_endpoint"],
        resource=discovered["resource"],
        client_id=client_id,
        scope=scope or None,
        expires_at=datetime.now(timezone.utc) + _TRANSACTION_TTL,
    )
    for replaced in replaced_transactions:
        await _destroy_secret_row(session, replaced)
    await McpServersRepo(session).update(
        server["id"], connection_status="connecting", enabled=False,
    )
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": discovered["resource"],
    }
    if scope:
        query["scope"] = scope
    return f"{discovered['authorization_endpoint']}?{urlencode(query)}"


async def complete_connection(
    session: AsyncSession, *, state: str, code: str,
) -> tuple[dict, dict]:
    oauth_repo = McpOAuthRepo(session)
    transaction = await oauth_repo.get_transaction(state_hash(state))
    if not transaction:
        raise ValueError("OAuth transaction is missing, expired, or already used")
    transaction_secrets = await _resolve_transaction_bundle(session, transaction)
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": transaction["redirect_uri"],
        "client_id": transaction["client_id"],
        "code_verifier": transaction_secrets["code_verifier"],
        "resource": transaction["resource"],
    }
    client_secret = transaction_secrets.get("client_secret")
    if client_secret:
        token_data["client_secret"] = client_secret
    timeout = httpx.Timeout(20.0, connect=6.0)
    response = await request_pinned_public_url(
        "POST",
        transaction["token_endpoint"],
        label="token endpoint",
        timeout=timeout,
        data=token_data,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        trusted_proxy_cidrs=_trusted_proxy_cidrs(),
    )
    response.raise_for_status()
    tokens = response.json()
    access_token = str(tokens.get("access_token") or "")
    if not access_token:
        raise ValueError("authorization server did not return an access token")
    expires_in = tokens.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
    existing_connection = await oauth_repo.get_connection(
        transaction["server_id"]
    )
    next_version = int(
        (existing_connection or {}).get("secret_version") or 0
    ) + 1
    connection_secret_ref = await secret_service().put_text(
        session,
        tenant_id=transaction["tenant_id"],
        purpose=_OAUTH_CONNECTION_PURPOSE,
        resource_type="mcp_installation",
        resource_id=transaction["server_id"],
        plaintext=_secret_payload({
            "client_secret": client_secret,
            "access_token": access_token,
            "refresh_token": tokens.get("refresh_token"),
        }),
        version=next_version,
    )
    previous_connection = await oauth_repo.upsert_connection(
        id=uuid.uuid4(),
        tenant_id=transaction["tenant_id"],
        user_id=transaction["user_id"],
        server_id=transaction["server_id"],
        authorization_server=transaction["authorization_server"],
        token_endpoint=transaction["token_endpoint"],
        revocation_endpoint=transaction.get("revocation_endpoint"),
        resource=transaction["resource"],
        client_id=transaction["client_id"],
        secret_ref=connection_secret_ref,
        secret_version=next_version,
        token_type=str(tokens.get("token_type") or "Bearer"),
        scope=str(tokens.get("scope") or transaction.get("scope") or "") or None,
        expires_at=expires_at,
    )
    await _destroy_secret_row(session, previous_connection)
    await delete_oauth_transaction(session, transaction["state_hash"])
    await McpServersRepo(session).update(
        transaction["server_id"], connection_status="connected", enabled=True,
        last_handshake_status="connected; tool discovery pending",
    )
    server = await McpServersRepo(session).get(transaction["server_id"])
    return transaction, server


async def resolve_oauth_auth_config(
    session: AsyncSession, server: dict,
) -> dict | None:
    if server.get("auth_mode") != "oauth":
        from vibecanvas_api.services.mcp_secret_config import (
            resolve_mcp_bearer_auth_config,
        )

        return await resolve_mcp_bearer_auth_config(session, server)
    connection = await McpOAuthRepo(session).get_connection(server["id"])
    if not connection:
        return None
    if connection.get("expires_at") and connection["expires_at"] <= datetime.now(timezone.utc):
        await McpServersRepo(session).update(
            server["id"], connection_status="reconnect_required", enabled=False,
        )
        return None
    token_bundle = await _resolve_connection_bundle(session, connection)
    return {"type": "bearer", "token": token_bundle["access_token"]}


async def disconnect(session: AsyncSession, server: dict) -> None:
    repo = McpOAuthRepo(session)
    connection = await repo.get_connection(server["id"])
    if connection and connection.get("revocation_endpoint"):
        token = (await _resolve_connection_bundle(session, connection)).get(
            "access_token"
        )
        if token:
            try:
                await request_pinned_public_url(
                    "POST",
                    connection["revocation_endpoint"],
                    label="revocation endpoint",
                    timeout=8.0,
                    data={"token": token, "client_id": connection["client_id"]},
                    headers={"User-Agent": _USER_AGENT},
                    trusted_proxy_cidrs=_trusted_proxy_cidrs(),
                )
            except (httpx.HTTPError, RuntimeError, ValueError):
                pass
    await delete_oauth_connection(session, server["id"])
    await McpServersRepo(session).update(
        server["id"], connection_status="connection_required", enabled=False,
        last_handshake_status=None, last_handshake_at=None,
        last_tool_count=None, last_tool_names=None,
    )
