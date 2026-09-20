"""Minimal enterprise OIDC Authorization Code + PKCE implementation.

The module deliberately owns one standards path instead of provider-specific
branches. Discovery, token, and JWKS requests all use the DNS-pinned public URL
transport; ID tokens are signature-checked and bound to issuer, audience,
nonce, and time before a local Session can be created.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.config import config
from vibecanvas_api.security.secret_service import secret_service
from vibecanvas_api.services.pinned_http import request_pinned_public_url
from vibecanvas_api.services.public_url import (
    PublicUrlError,
    validate_public_http_url,
)
from vibecanvas_api.storage.models_enterprise_identity import (
    EnterpriseIdentityProvider,
    OidcLoginTransaction,
)


_CLAIM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_TRANSACTION_TTL = timedelta(minutes=5)
_USER_AGENT = "flowork/1.0 enterprise OIDC"
_MAX_METADATA_BYTES = 256 * 1024
_MAX_TOKEN_BYTES = 128 * 1024
_MAX_CLOCK_SKEW_SECONDS = 60


class OidcError(ValueError):
    """Stable, content-free OIDC failure surfaced by the auth route."""


@dataclass(frozen=True, slots=True)
class OidcMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    token_endpoint_auth_methods_supported: tuple[str, ...] = (
        "client_secret_basic",
    )


@dataclass(frozen=True, slots=True)
class OidcTransactionBundle:
    code_verifier: str
    nonce: str


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > _MAX_TOKEN_BYTES:
        raise OidcError("oidc_token_invalid")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise OidcError("oidc_token_invalid") from exc


def _canonical_issuer(value: str) -> str:
    parts = urlsplit(str(value or "").strip())
    if parts.query or parts.fragment:
        raise OidcError("oidc_issuer_invalid")
    return str(value or "").strip().rstrip("/")


async def _public_https_url(value: str, *, label: str) -> str:
    try:
        target = await validate_public_http_url(
            value,
            label=label,
            require_https=True,
        )
    except PublicUrlError as exc:
        raise OidcError("oidc_metadata_invalid") from exc
    return target.url.rstrip("/") if label == "OIDC issuer" else target.url


async def _get_json(url: str, *, label: str) -> dict[str, Any]:
    try:
        response = await request_pinned_public_url(
            "GET",
            url,
            label=label,
            timeout=httpx.Timeout(15.0, connect=6.0),
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            max_response_bytes=_MAX_METADATA_BYTES,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, PublicUrlError) as exc:
        raise OidcError("oidc_metadata_unavailable") from exc
    if not isinstance(payload, dict):
        raise OidcError("oidc_metadata_invalid")
    return payload


async def discover_oidc(issuer_url: str) -> OidcMetadata:
    issuer = await _public_https_url(
        _canonical_issuer(issuer_url),
        label="OIDC issuer",
    )
    metadata = await _get_json(
        f"{issuer}/.well-known/openid-configuration",
        label="OIDC discovery document",
    )
    if _canonical_issuer(str(metadata.get("issuer") or "")) != issuer:
        raise OidcError("oidc_issuer_mismatch")
    response_types = metadata.get("response_types_supported") or []
    if "code" not in response_types:
        raise OidcError("oidc_authorization_code_unsupported")
    challenge_methods = metadata.get("code_challenge_methods_supported")
    if challenge_methods is not None and "S256" not in challenge_methods:
        raise OidcError("oidc_pkce_s256_unsupported")
    raw_auth_methods = metadata.get("token_endpoint_auth_methods_supported")
    if raw_auth_methods is None:
        auth_methods = ("client_secret_basic",)
    elif isinstance(raw_auth_methods, list):
        auth_methods = tuple(
            method for method in raw_auth_methods
            if method in {"client_secret_basic", "client_secret_post", "none"}
        )
    else:
        raise OidcError("oidc_metadata_invalid")
    if not auth_methods:
        raise OidcError("oidc_token_auth_method_unsupported")
    return OidcMetadata(
        issuer=issuer,
        authorization_endpoint=await _public_https_url(
            str(metadata.get("authorization_endpoint") or ""),
            label="OIDC authorization endpoint",
        ),
        token_endpoint=await _public_https_url(
            str(metadata.get("token_endpoint") or ""),
            label="OIDC token endpoint",
        ),
        jwks_uri=await _public_https_url(
            str(metadata.get("jwks_uri") or ""),
            label="OIDC JWKS endpoint",
        ),
        token_endpoint_auth_methods_supported=auth_methods,
    )


def validate_claim_names(provider: EnterpriseIdentityProvider) -> None:
    if any(
        not _CLAIM_NAME.fullmatch(value)
        for value in (
            provider.subject_claim,
            provider.email_claim,
            provider.display_name_claim,
        )
    ):
        raise OidcError("oidc_claim_mapping_invalid")


def validate_return_to(value: str) -> str:
    candidate = str(value or "/").strip()
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or len(candidate) > 2048
    ):
        raise OidcError("oidc_return_to_invalid")
    return candidate


def _callback_url() -> str:
    try:
        return config.public_urls.absolute("api/v1/auth/sso/callback")
    except ValueError as exc:
        raise OidcError("oidc_public_url_required") from exc


async def create_login_transaction(
    session: AsyncSession,
    *,
    provider: EnterpriseIdentityProvider,
    return_to: str,
) -> tuple[str, str]:
    validate_claim_names(provider)
    metadata = await discover_oidc(provider.issuer_url)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state.encode("ascii")).hexdigest()
    secret_ref = await secret_service().put_text(
        session,
        tenant_id=provider.tenant_id,
        purpose="oidc_login_transaction",
        resource_type="enterprise_identity_provider",
        resource_id=state_hash,
        plaintext=json.dumps(
            {"code_verifier": verifier, "nonce": nonce},
            separators=(",", ":"),
        ),
    )
    session.add(OidcLoginTransaction(
        state_hash=state_hash,
        provider_id=provider.provider_id,
        tenant_id=provider.tenant_id,
        secret_ref=secret_ref,
        return_to=validate_return_to(return_to),
        expires_at=datetime.now(timezone.utc) + _TRANSACTION_TTL,
    ))
    await session.flush()
    callback_url = _callback_url()
    query = urlencode({
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": callback_url,
        "scope": " ".join(dict.fromkeys(provider.scopes)),
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    })
    return state_hash, f"{metadata.authorization_endpoint}?{query}"


async def resolve_transaction_bundle(
    session: AsyncSession,
    transaction: OidcLoginTransaction,
) -> OidcTransactionBundle:
    raw = await secret_service().resolve_text(
        session,
        secret_ref=transaction.secret_ref,
        tenant_id=transaction.tenant_id,
        purpose="oidc_login_transaction",
        resource_type="enterprise_identity_provider",
        resource_id=transaction.state_hash,
    )
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise OidcError("oidc_transaction_invalid") from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("code_verifier"), str)
        or not isinstance(value.get("nonce"), str)
    ):
        raise OidcError("oidc_transaction_invalid")
    return OidcTransactionBundle(
        code_verifier=value["code_verifier"],
        nonce=value["nonce"],
    )


async def _client_secret(
    session: AsyncSession,
    provider: EnterpriseIdentityProvider,
) -> str | None:
    if provider.client_secret_ref is None:
        return None
    return await secret_service().resolve_text(
        session,
        secret_ref=provider.client_secret_ref,
        tenant_id=provider.tenant_id,
        purpose="oidc_client_secret",
        resource_type="enterprise_identity_provider",
        resource_id=provider.provider_id,
    )


async def exchange_code(
    session: AsyncSession,
    *,
    provider: EnterpriseIdentityProvider,
    code: str,
    bundle: OidcTransactionBundle,
) -> tuple[str, OidcMetadata]:
    if not code or len(code) > 8192:
        raise OidcError("oidc_code_invalid")
    metadata = await discover_oidc(provider.issuer_url)
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _callback_url(),
        "code_verifier": bundle.code_verifier,
    }
    client_secret = await _client_secret(session, provider)
    auth_method = provider.token_endpoint_auth_method
    if auth_method not in metadata.token_endpoint_auth_methods_supported:
        raise OidcError("oidc_token_auth_method_unsupported")
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if auth_method == "client_secret_basic":
        if not client_secret:
            raise OidcError("oidc_client_secret_missing")
        basic = base64.b64encode(
            f"{provider.client_id}:{client_secret}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
    elif auth_method == "client_secret_post":
        if not client_secret:
            raise OidcError("oidc_client_secret_missing")
        token_data["client_id"] = provider.client_id
        token_data["client_secret"] = client_secret
    elif auth_method == "none":
        if client_secret:
            raise OidcError("oidc_public_client_secret_forbidden")
        token_data["client_id"] = provider.client_id
    else:
        raise OidcError("oidc_token_auth_method_unsupported")
    try:
        response = await request_pinned_public_url(
            "POST",
            metadata.token_endpoint,
            label="OIDC token endpoint",
            timeout=httpx.Timeout(20.0, connect=6.0),
            data=token_data,
            headers=headers,
            max_response_bytes=_MAX_TOKEN_BYTES,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, PublicUrlError) as exc:
        raise OidcError("oidc_token_exchange_failed") from exc
    id_token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise OidcError("oidc_id_token_missing")
    return id_token, metadata


def _hash_for_alg(alg: str):
    mapping = {
        "RS256": hashes.SHA256,
        "RS384": hashes.SHA384,
        "RS512": hashes.SHA512,
        "ES256": hashes.SHA256,
        "ES384": hashes.SHA384,
        "ES512": hashes.SHA512,
    }
    factory = mapping.get(alg)
    if factory is None:
        raise OidcError("oidc_signing_algorithm_unsupported")
    return factory()


def _jwk_public_key(jwk: dict[str, Any], alg: str):
    if alg.startswith("RS") and jwk.get("kty") == "RSA":
        try:
            n = int.from_bytes(_b64decode(str(jwk["n"])), "big")
            e = int.from_bytes(_b64decode(str(jwk["e"])), "big")
            return rsa.RSAPublicNumbers(e=e, n=n).public_key()
        except (KeyError, TypeError, ValueError) as exc:
            raise OidcError("oidc_jwk_invalid") from exc
    if alg.startswith("ES") and jwk.get("kty") == "EC":
        curves = {
            "ES256": ("P-256", ec.SECP256R1),
            "ES384": ("P-384", ec.SECP384R1),
            "ES512": ("P-521", ec.SECP521R1),
        }
        expected_curve, curve_type = curves[alg]
        if jwk.get("crv") != expected_curve:
            raise OidcError("oidc_jwk_invalid")
        try:
            x = int.from_bytes(_b64decode(str(jwk["x"])), "big")
            y = int.from_bytes(_b64decode(str(jwk["y"])), "big")
            return ec.EllipticCurvePublicNumbers(
                x=x,
                y=y,
                curve=curve_type(),
            ).public_key()
        except (KeyError, TypeError, ValueError) as exc:
            raise OidcError("oidc_jwk_invalid") from exc
    raise OidcError("oidc_jwk_invalid")


def _json_segment(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(_b64decode(value))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("oidc_token_invalid") from exc
    if not isinstance(payload, dict):
        raise OidcError("oidc_token_invalid")
    return payload


def _verify_signature(
    *,
    token: str,
    header: dict[str, Any],
    jwks: dict[str, Any],
) -> None:
    parts = token.split(".")
    if len(parts) != 3:
        raise OidcError("oidc_token_invalid")
    alg = str(header.get("alg") or "")
    kid = header.get("kid")
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise OidcError("oidc_jwks_invalid")
    candidates = [
        key for key in keys
        if isinstance(key, dict)
        and (kid is None or key.get("kid") == kid)
        and (key.get("use") in {None, "sig"})
        and (key.get("alg") in {None, alg})
    ]
    if len(candidates) != 1:
        raise OidcError("oidc_signing_key_unavailable")
    public_key = _jwk_public_key(candidates[0], alg)
    signature = _b64decode(parts[2])
    signed = f"{parts[0]}.{parts[1]}".encode("ascii")
    algorithm = _hash_for_alg(alg)
    try:
        if alg.startswith("RS"):
            public_key.verify(signature, signed, padding.PKCS1v15(), algorithm)
        else:
            width = (public_key.curve.key_size + 7) // 8
            if len(signature) != width * 2:
                raise OidcError("oidc_token_invalid")
            der = encode_dss_signature(
                int.from_bytes(signature[:width], "big"),
                int.from_bytes(signature[width:], "big"),
            )
            public_key.verify(der, signed, ec.ECDSA(algorithm))
    except OidcError:
        raise
    except Exception as exc:
        raise OidcError("oidc_signature_invalid") from exc


async def validate_id_token(
    *,
    id_token: str,
    metadata: OidcMetadata,
    provider: EnterpriseIdentityProvider,
    nonce: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if len(id_token) > _MAX_TOKEN_BYTES:
        raise OidcError("oidc_token_invalid")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise OidcError("oidc_token_invalid")
    header = _json_segment(parts[0])
    claims = _json_segment(parts[1])
    jwks = await _get_json(metadata.jwks_uri, label="OIDC JWKS endpoint")
    _verify_signature(token=id_token, header=header, jwks=jwks)

    current = (now or datetime.now(timezone.utc)).timestamp()
    try:
        exp = float(claims["exp"])
        iat = float(claims["iat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OidcError("oidc_token_time_invalid") from exc
    nbf = claims.get("nbf")
    if exp <= current - _MAX_CLOCK_SKEW_SECONDS:
        raise OidcError("oidc_token_expired")
    if iat > current + _MAX_CLOCK_SKEW_SECONDS:
        raise OidcError("oidc_token_time_invalid")
    if nbf is not None:
        try:
            if float(nbf) > current + _MAX_CLOCK_SKEW_SECONDS:
                raise OidcError("oidc_token_not_yet_valid")
        except (TypeError, ValueError) as exc:
            raise OidcError("oidc_token_time_invalid") from exc
    if _canonical_issuer(str(claims.get("iss") or "")) != metadata.issuer:
        raise OidcError("oidc_issuer_mismatch")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or provider.client_id not in audiences:
        raise OidcError("oidc_audience_mismatch")
    if len(audiences) > 1 and claims.get("azp") != provider.client_id:
        raise OidcError("oidc_authorized_party_mismatch")
    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise OidcError("oidc_nonce_mismatch")
    return claims


def mapped_identity_claims(
    provider: EnterpriseIdentityProvider,
    claims: dict[str, Any],
) -> tuple[str, str, str]:
    validate_claim_names(provider)
    subject = claims.get(provider.subject_claim)
    email = claims.get(provider.email_claim)
    display_name = claims.get(provider.display_name_claim)
    if not isinstance(subject, str) or not subject or len(subject) > 1024:
        raise OidcError("oidc_subject_missing")
    if not isinstance(email, str) or not email or len(email) > 320:
        raise OidcError("oidc_email_missing")
    if display_name is None:
        display_name = email
    if not isinstance(display_name, str) or len(display_name) > 256:
        raise OidcError("oidc_display_name_invalid")
    return subject, email.strip(), display_name.strip() or email.strip()


__all__ = [
    "OidcError",
    "OidcMetadata",
    "OidcTransactionBundle",
    "create_login_transaction",
    "discover_oidc",
    "exchange_code",
    "mapped_identity_claims",
    "resolve_transaction_bundle",
    "validate_claim_names",
    "validate_id_token",
    "validate_return_to",
]
