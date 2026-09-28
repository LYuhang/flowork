"""Host-side broker for credential-bearing custom remote MCP servers.

The Chat sandbox receives only a Turn-scoped capability and this internal
endpoint.  Every request revalidates the browser Session, organization
membership, active Agent Run, current Runtime binding, Chat execute access,
and private MCP installation use access before resolving any stored secret.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.auth.live_identity import (
    LiveIdentityError,
    resolve_live_authorization_identity,
)
from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
    scope_authz_service,
)
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.security.secret_service import SecretServiceError
from vibecanvas_api.services.agent_runtime.custom_mcp_capability import (
    RuntimeCustomMcpCapability,
    mcp_config_revision,
    verify_runtime_custom_mcp_capability,
)
from vibecanvas_api.services.agent_runtime.model_capability import (
    authorization_model_generation,
)
from vibecanvas_api.services.mcp_config import normalize_transport, server_descriptor
from vibecanvas_api.services.mcp_connection_secrets import (
    hydrate_connection_credentials,
)
from vibecanvas_api.services.mcp_oauth import resolve_oauth_auth_config
from vibecanvas_api.services.pinned_http import PinnedAsyncHTTPTransport
from vibecanvas_api.services.public_url import PublicUrlError, validate_public_http_url
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import Chat
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo

router = APIRouter(tags=["runtime-mcp-broker"])
logger = structlog.get_logger(__name__)

_MAX_CAPABILITY_BYTES = 16 * 1024
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_TARGET_URL_BYTES = 16 * 1024
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})
_SAFE_INBOUND_HEADERS = frozenset({
    "accept",
    "accept-encoding",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
    "user-agent",
})
_SAFE_RESPONSE_HEADERS = frozenset({
    "cache-control",
    "content-encoding",
    "content-type",
    "etag",
    "last-modified",
    "mcp-protocol-version",
    "mcp-session-id",
    "retry-after",
    "x-accel-buffering",
})


@dataclass(frozen=True, slots=True)
class RuntimeMcpTarget:
    url: str
    headers: dict[str, str]
    addresses: dict[str, tuple[str, ...]]


def _extract_capability(request: Request) -> str:
    value = request.headers.get("authorization", "").strip()
    if not value.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_mcp_capability_invalid"},
        )
    token = value[7:].strip()
    if not token or len(token.encode("utf-8")) > _MAX_CAPABILITY_BYTES:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_mcp_capability_invalid"},
        )
    return token


async def _bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"code": "runtime_mcp_request_too_large"},
            )
        body.extend(chunk)
    return bytes(body)


def _safe_stored_headers(connection: dict[str, Any]) -> dict[str, str]:
    raw = connection.get("headers") or {}
    if not isinstance(raw, dict):
        raise TypeError("invalid MCP headers")
    result: dict[str, str] = {}
    for raw_name, raw_value in raw.items():
        name = str(raw_name)
        value = str(raw_value)
        lower = name.casefold()
        if (
            not _HEADER_NAME.fullmatch(name)
            or lower in _HOP_BY_HOP
            or lower in {"host", "content-length", "cookie", "set-cookie"}
            or lower.startswith(("proxy-", "x-forwarded-"))
            or "\r" in value
            or "\n" in value
            or len(value.encode("utf-8")) > 16 * 1024
        ):
            raise ValueError("unsafe MCP header")
        result[name] = value
    return result


def _forward_headers(
    request: Request,
    *,
    target_headers: dict[str, str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in request.headers.items():
        if name.casefold() in _SAFE_INBOUND_HEADERS:
            result[name] = value
    # Stored headers win over protocol headers with the same spelling. In
    # particular, this replaces the broker capability Authorization value with
    # the real remote credential.
    result.update(target_headers)
    return result


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.casefold() in _SAFE_RESPONSE_HEADERS
        and name.casefold() not in _HOP_BY_HOP
    }


def _target_url(base_url: str, request: Request) -> str:
    parts = urlsplit(base_url)
    configured_query = list(parse_qsl(parts.query, keep_blank_values=True))
    configured_names = {name.casefold() for name, _ in configured_query}
    # Configuration query values may contain credentials. A sandbox request
    # cannot override them by appending a duplicate parameter.
    incoming_query = [
        (name, value)
        for name, value in request.query_params.multi_items()
        if name.casefold() not in configured_names
    ]
    target = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode([*configured_query, *incoming_query], doseq=True),
            "",
        )
    )
    if len(target.encode("utf-8")) > _MAX_TARGET_URL_BYTES:
        raise HTTPException(
            status_code=414,
            detail={"code": "runtime_mcp_target_too_long"},
        )
    return target


def _authz_context(
    capability: RuntimeCustomMcpCapability,
    *,
    auth: AuthContext,
) -> AuthzRequestContext:
    return AuthzRequestContext(
        active_organization_id=capability.organization_id,
        request_id=f"runtime-mcp:{capability.turn_id}",
        session_id=capability.session_id,
        session_generation=capability.session_generation,
        membership_id=capability.membership_id,
        membership_role=auth.membership_role,
        membership_status=auth.membership_status,
        authentication_strength=auth.authentication_strength,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )


async def _authorize_and_resolve_target(
    request: Request,
    capability: RuntimeCustomMcpCapability,
) -> RuntimeMcpTarget:
    try:
        uuid.UUID(capability.session_id)
        uuid.UUID(capability.user_id)
        uuid.UUID(capability.organization_id)
        server_uuid = uuid.UUID(capability.server_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_mcp_capability_invalid"},
        ) from exc

    expected_generation = authorization_model_generation(
        model_id=config.openfga_authorization_model_id,
    )
    if capability.authorization_generation != expected_generation:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_mcp_authorization_generation_stale"},
        )

    async with session_scope() as identity_session:
        try:
            live_auth = await resolve_live_authorization_identity(
                identity_session,
                session_id=capability.session_id,
                user_id=capability.user_id,
                organization_id=capability.organization_id,
                session_generation=capability.session_generation,
                membership_id=capability.membership_id,
            )
        except LiveIdentityError as exc:
            code = (
                "runtime_mcp_membership_revoked"
                if "membership" in exc.reason
                else "runtime_mcp_session_revoked"
            )
            raise HTTPException(
                status_code=403,
                detail={"code": code},
            ) from exc

    async with session_scope(tenant_id=capability.organization_id, user_id=capability.user_id) as session:
        run = await AgentRunsRepo(session).get_for_chat(
            capability.chat_id,
            capability.turn_id,
            creator_user_id=capability.user_id,
        )
        chat = await session.get(Chat, capability.chat_id)
        if (
            run is None
            or run.status != "running"
            or chat is None
            or chat.project.runtime_session_id != capability.runtime_session_id
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "runtime_mcp_turn_inactive"},
            )

        service = authz_service_for_session(
            session=session,
            organization_id=capability.organization_id,
            openfga_client=getattr(request.app.state, "openfga_client", None),
        )
        service = scope_authz_service(
            service,
            session=session,
            auth=live_auth,
            request=request,
        )
        principal = PrincipalRef(PrincipalType.USER, capability.user_id)
        authz_context = _authz_context(
            capability,
            auth=live_auth,
        )
        for action, resource_type, resource_id, error_code in (
            (
                Action.EXECUTE,
                ResourceType.CHAT,
                capability.chat_id,
                "runtime_mcp_chat_access_revoked",
            ),
            (
                Action.USE,
                ResourceType.MCP_INSTALLATION,
                capability.server_id,
                "runtime_mcp_installation_access_revoked",
            ),
        ):
            decision = await service.check(
                principal,
                action,
                ResourceRef(
                    resource_type,
                    resource_id,
                    capability.organization_id,
                ),
                authz_context,
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail={"code": error_code})

        row = await McpServersRepo(session).get_for_user(
            server_uuid,
            capability.user_id,
        )
        if (
            row is None
            or not row.get("enabled")
            or row.get("connection_status")
            not in {"not_required", "connected"}
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "runtime_mcp_installation_unavailable"},
            )
        revision = mcp_config_revision(
            server_id=row["id"],
            updated_at=row.get("updated_at"),
        )
        transport = normalize_transport(str(row.get("transport") or ""))
        if (
            transport == "stdio"
            or transport != capability.transport
            or revision != capability.config_revision
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "runtime_mcp_configuration_stale"},
            )

        hydrated = await hydrate_connection_credentials(session, row)
        auth_config = await resolve_oauth_auth_config(session, hydrated)
        if auth_config is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "runtime_mcp_authorization_required"},
            )
        descriptor = server_descriptor(
            {**hydrated, "auth_config": auth_config}
        )
        connection = descriptor["connection"]
        if normalize_transport(str(connection.get("transport") or "")) != transport:
            raise HTTPException(
                status_code=409,
                detail={"code": "runtime_mcp_configuration_stale"},
            )
        target_url = str(connection.get("url") or "")
        try:
            target = await validate_public_http_url(
                target_url,
                label="remote MCP endpoint",
                require_https=True,
                trusted_proxy_cidrs=(
                    config.sandbox_egress_trusted_proxy_cidrs
                    if config.sandbox_egress_mode == "proxy"
                    else ()
                ),
            )
            target_headers = _safe_stored_headers(connection)
        except (PublicUrlError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "runtime_mcp_destination_unsafe"},
            ) from exc

    return RuntimeMcpTarget(
        url=target.url,
        headers=target_headers,
        addresses={target.hostname: target.addresses},
    )


async def _proxy_runtime_mcp_request(request: Request, server_id: str):
    token = _extract_capability(request)
    capability = verify_runtime_custom_mcp_capability(
        token,
        secret=config.signing_secret,
        server_id=server_id,
    )
    if capability is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_mcp_capability_invalid"},
        )
    try:
        target = await _authorize_and_resolve_target(request, capability)
    except SecretServiceError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "runtime_mcp_credential_unavailable"},
        ) from exc

    body = await _bounded_body(request)
    target_url = _target_url(target.url, request)
    transport = PinnedAsyncHTTPTransport(addresses=target.addresses)
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=10.0, read=24 * 60 * 60, write=60.0, pool=10.0),
        follow_redirects=False,
        trust_env=False,
    )
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                target_url,
                headers=_forward_headers(
                    request,
                    target_headers=target.headers,
                ),
                content=body if body else None,
            ),
            stream=True,
        )
    except Exception as exc:
        await client.aclose()
        logger.warning(
            "runtime_mcp_upstream_unavailable",
            chat_id=capability.chat_id,
            turn_id=capability.turn_id,
            server_id=capability.server_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail={"code": "runtime_mcp_upstream_unavailable"},
        ) from exc
    if 300 <= upstream.status_code < 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail={"code": "runtime_mcp_redirect_denied"},
        )

    async def stream_body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=None,
    )


@router.api_route(
    "/api/internal/runtime-mcp/v1/{server_id}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def runtime_mcp_request(request: Request, server_id: str):
    return await _proxy_runtime_mcp_request(request, server_id)


__all__ = ["RuntimeMcpTarget", "router"]
