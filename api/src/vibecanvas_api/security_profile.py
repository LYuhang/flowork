"""Fail-closed deployment profile validation.

Development conveniences are intentionally useful in a source checkout, but
none of them may become an implicit production fallback.  This module keeps
the production gate independent from individual feature routers so API,
worker, and future management commands can share the same deterministic
validation.

The validator reports stable, non-secret issue codes.  It never includes DSNs,
tokens, keys, or other configuration values in exceptions or logs.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import re
from urllib.parse import parse_qs, urlsplit

from .config import AppConfig


@dataclass(frozen=True, slots=True)
class SecurityProfileIssue:
    code: str
    detail: str


class ProductionSecurityError(RuntimeError):
    """Raised when a production process is configured to fail open."""

    def __init__(self, issues: tuple[SecurityProfileIssue, ...]):
        self.issues = issues
        codes = ", ".join(issue.code for issue in issues)
        super().__init__(f"production security profile rejected: {codes}")


def configured_cors_origins() -> list[str]:
    """Read the single deployment-owned CORS origin list."""
    raw = os.getenv("VIBECANVAS_API_CORS_ORIGINS", "http://localhost:3000")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _url_is_https(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname)
    except ValueError:
        return False


def _url_uses_local_host(value: str) -> bool:
    try:
        hostname = (urlsplit(value).hostname or "").rstrip(".").lower()
    except ValueError:
        return True
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return not address.is_global


def _database_tls_is_verified(value: str) -> bool:
    try:
        query = parse_qs(urlsplit(value).query)
    except ValueError:
        return False
    modes = {
        item.strip().lower() for item in query.get("sslmode", []) + query.get("ssl", [])
    }
    return bool(modes & {"verify-full", "verify-ca"})


def _redis_uses_tls(value: str) -> bool:
    try:
        return urlsplit(value).scheme == "rediss"
    except ValueError:
        return False


def _has_nondefault_database_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    username = (parsed.username or "").lower()
    password = parsed.password or ""
    return bool(
        username
        and password
        and username not in {"dev", "postgres", "root"}
        and password.lower()
        not in {
            "dev",
            "postgres",
            "password",
            "changeme",
            "change-me",
        }
    )


def _has_nondefault_redis_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    password = parsed.password or ""
    return bool(
        password
        and password.lower()
        not in {
            "dev",
            "redis",
            "password",
            "changeme",
            "change-me",
        }
    )


def _cors_issues(origins: list[str]) -> list[SecurityProfileIssue]:
    issues: list[SecurityProfileIssue] = []
    if not origins:
        return [
            SecurityProfileIssue(
                "cors.origins_missing",
                "production requires an explicit HTTPS CORS origin list",
            )
        ]
    for origin in origins:
        if "*" in origin:
            issues.append(
                SecurityProfileIssue(
                    "cors.wildcard",
                    "production CORS origins cannot contain a wildcard",
                )
            )
            continue
        if not _url_is_https(origin):
            issues.append(
                SecurityProfileIssue(
                    "cors.origin_not_public_https",
                    "production CORS origins must be exact HTTPS origins",
                )
            )
    return issues


def production_security_issues(
    config: AppConfig,
    *,
    cors_origins: list[str],
) -> tuple[SecurityProfileIssue, ...]:
    """Return every production profile violation without exposing values."""
    if config.environment != "production":
        return ()

    issues: list[SecurityProfileIssue] = []

    def require(condition: bool, code: str, detail: str) -> None:
        if not condition:
            issues.append(SecurityProfileIssue(code, detail))

    public_url = config.public_urls.public_url
    require(
        _url_is_https(public_url),
        "public_url.public_https_required",
        "VIBECANVAS_PUBLIC_URL must be an exact HTTPS URL",
    )
    require(
        _database_tls_is_verified(config.database.url),
        "database.tls_verification_required",
        "DATABASE_URL must verify the database TLS certificate",
    )
    require(
        _has_nondefault_database_credentials(config.database.url),
        "database.nondefault_credentials_required",
        "DATABASE_URL must use non-default credentials",
    )
    require(
        not config.run_database_migrations,
        "database.runtime_migrations_forbidden",
        "production API/worker processes must not run schema migrations",
    )
    maintenance_url = os.environ.get("MAINTENANCE_DATABASE_URL", "")
    require(
        not os.environ.get("MIGRATION_DATABASE_URL"),
        "database.migration_dsn_forbidden",
        "production runtime processes must not receive migration credentials",
    )
    require(
        not os.environ.get("ADMIN_DATABASE_URL"),
        "database.legacy_admin_dsn_forbidden",
        "use a restricted MAINTENANCE_DATABASE_URL, never a superuser DSN",
    )
    require(
        _database_tls_is_verified(maintenance_url),
        "database.maintenance_tls_verification_required",
        "MAINTENANCE_DATABASE_URL must verify database TLS",
    )
    require(
        _has_nondefault_database_credentials(maintenance_url),
        "database.maintenance_credentials_required",
        "MAINTENANCE_DATABASE_URL must use non-default credentials",
    )
    require(
        _redis_uses_tls(config.redis.url),
        "redis.tls_required",
        "REDIS_URL must use rediss:// in production",
    )
    require(
        _has_nondefault_redis_credentials(config.redis.url),
        "redis.nondefault_credentials_required",
        "REDIS_URL must include non-default credentials",
    )
    require(
        not config._signing_secret_is_ephemeral
        and len(config.signing_secret.encode("utf-8")) >= 32,
        "signing.stable_secret_required",
        "VIBECANVAS_SIGNING_SECRET must be stable and at least 32 bytes",
    )
    require(
        config.browser_token_secret != "dev-insecure-browser-secret-change-me"
        and len(config.browser_token_secret.encode("utf-8")) >= 32,
        "browser.stable_token_secret_required",
        "BROWSER_TOKEN_SECRET must be stable and at least 32 bytes",
    )
    require(
        bool(re.fullmatch(r"[a-p]{32}", config.browser_extension_id)),
        "browser.extension_id_required",
        "VIBECANVAS_BROWSER_EXTENSION_ID must be a published Chrome extension id",
    )
    require(
        _url_is_https(config.openfga_api_url)
        and not _url_uses_local_host(config.openfga_api_url),
        "openfga.public_https_required",
        "OPENFGA_API_URL must be a non-local HTTPS service URL",
    )
    require(
        bool(config.openfga_store_id),
        "openfga.store_id_required",
        "OPENFGA_STORE_ID is required",
    )
    require(
        bool(config.openfga_authorization_model_id),
        "openfga.model_id_required",
        "OPENFGA_AUTHORIZATION_MODEL_ID is required",
    )
    require(
        bool(config.openfga_api_token),
        "openfga.authentication_required",
        "OpenFGA authentication is required",
    )
    require(
        bool(config.kms_provider), "kms.provider_required", "KMS_PROVIDER is required"
    )
    require(
        len(config.content_lookup_hmac_key.encode("utf-8")) >= 32,
        "content.lookup_hmac_key_required",
        "CONTENT_LOOKUP_HMAC_KEY must be stable and at least 32 bytes",
    )
    require(
        config.kms_provider.lower() == "aws-kms",
        "kms.managed_provider_required",
        "production must use the supported aws-kms provider",
    )
    require(bool(config.kms_key_id), "kms.key_id_required", "KMS_KEY_ID is required")
    require(
        bool(config.kms_workload_identity),
        "kms.workload_identity_required",
        "KMS workload identity is required",
    )
    require(
        not config.aws_static_credentials_present,
        "kms.static_aws_credentials_forbidden",
        "production must use AWS workload identity, not static access keys",
    )
    require(bool(config.smtp_host), "smtp.host_required", "SMTP_HOST is required")
    require(bool(config.smtp_user), "smtp.user_required", "SMTP_USER is required")
    require(
        bool(config.smtp_password_secret_id),
        "smtp.managed_secret_required",
        "SMTP_PASSWORD_SECRET_ID is required",
    )
    require(
        not config.smtp_plaintext_password_present,
        "smtp.plaintext_password_forbidden",
        "production must not use SMTP_PASSWORD",
    )
    require(
        config.object_store.provider == "s3",
        "object_store.production_provider_required",
        "production object storage must use the s3 provider",
    )
    require(
        bool(config.object_store.s3_bucket),
        "object_store.bucket_required",
        "S3_BUCKET is required",
    )
    require(
        config.object_store.s3_server_side_encryption == "aws:kms",
        "object_store.kms_encryption_required",
        "production object storage must use SSE-KMS",
    )
    require(
        bool(config.object_store.s3_kms_key_id),
        "object_store.kms_key_required",
        "S3_KMS_KEY_ID is required",
    )
    require(
        not config.object_store.s3_access_key and not config.object_store.s3_secret_key,
        "object_store.workload_identity_required",
        "production S3 access must use workload identity, not static keys",
    )
    require(
        config.sandbox_service_mode == "service",
        "sandbox.independent_service_required",
        "production must use the independently supervised Sandbox Service",
    )
    require(
        config.sandbox_service_endpoint.startswith(("unix://", "grpcs://")),
        "sandbox.secure_endpoint_required",
        "Sandbox Service must use a private Unix socket or mTLS gRPC",
    )
    if config.sandbox_service_endpoint.startswith("grpcs://"):
        require(
            bool(config.sandbox_service_ca_file),
            "sandbox.client_ca_required",
            "remote Sandbox Service requires a trusted CA",
        )
        require(
            bool(config.sandbox_service_cert_file)
            and bool(config.sandbox_service_key_file),
            "sandbox.client_identity_required",
            "remote Sandbox Service requires a workload client certificate and key",
        )
    require(
        bool(config.audit_export_url) and _url_is_https(config.audit_export_url),
        "audit.export_required",
        "an HTTPS immutable audit export destination is required",
    )
    require(
        config.backup_encryption_verified,
        "backup.encryption_verification_required",
        "backup encryption and restore verification must be enabled",
    )
    require(
        config.purge_worker_enabled,
        "purge.worker_required",
        "the durable purge worker must be enabled",
    )
    require(
        config.distributed_auth_rate_limit_enabled,
        "auth.distributed_rate_limit_required",
        "distributed login/reset rate limiting must be enabled",
    )
    require(
        config.high_risk_step_up_required,
        "auth.high_risk_step_up_required",
        "production high-risk mutations must require recent WebAuthn step-up",
    )
    require(
        not config.privileged_access_enabled
        or len(config.privileged_support_operator_ids) >= 2,
        "auth.privileged_access_two_person_required",
        "enabled privileged access requires at least two eligible operators",
    )
    require(
        config.webauthn_origin.startswith("https://"),
        "auth.webauthn_https_required",
        "production WebAuthn origin must use HTTPS",
    )
    require(
        config.web_session_cookie_enabled,
        "session.secure_cookie_required",
        "the primary Web session must use the secure-cookie implementation",
    )
    require(
        config.extension_scoped_token_enabled,
        "browser.scoped_exchange_required",
        "the extension must use one-time scoped-token exchange",
    )
    require(
        config.upload_scanner_provider == "clamd",
        "upload.malware_scanner_required",
        "production file ingress must use the clamd scanner",
    )
    require(
        bool(config.upload_scanner_clamd_unix_socket)
        and os.path.isabs(config.upload_scanner_clamd_unix_socket),
        "upload.scanner_unix_socket_required",
        "production clamd must use an absolute local Unix socket path",
    )
    require(
        bool(config.trusted_proxy_cidrs),
        "proxy.trusted_cidrs_required",
        "TRUSTED_PROXY_CIDRS must define the deployment proxy boundary",
    )
    require(
        not config.enable_test_user,
        "debug.test_user_forbidden",
        "ENABLE_TEST_USER must be disabled",
    )
    require(
        not config.agent_debug_view_enabled,
        "debug.inspector_forbidden",
        "plaintext Agent Debug/Inspector must be disabled",
    )
    require(
        not config.sandbox_debug_execute_enabled,
        "debug.sandbox_execute_forbidden",
        "sandbox debug-execute must be disabled",
    )
    require(
        bool(config.security_frame_ancestors)
        and "*" not in config.security_frame_ancestors,
        "headers.frame_ancestors_required",
        "production frame-ancestors must be explicit and cannot contain wildcard",
    )
    issues.extend(_cors_issues(cors_origins))

    # Stable ordering keeps startup logs and tests deterministic.
    return tuple(sorted(set(issues), key=lambda issue: issue.code))


def validate_production_security(
    config: AppConfig,
    *,
    cors_origins: list[str],
) -> None:
    issues = production_security_issues(config, cors_origins=cors_origins)
    if issues:
        raise ProductionSecurityError(issues)


def validate_browser_gateway_security(config: AppConfig) -> None:
    """Apply shared transport/identity rules to the connection-only process.

    It has no file ingress, storage, maintenance pool or HTTP cookie surface;
    requiring those API capabilities would grant the gateway unnecessary access.
    Keep the underlying checks identical to the application's production rules.
    """
    issues = tuple(
        issue for issue in production_security_issues(config, cors_origins=[])
        if issue.code.startswith(("database.", "redis.", "signing.", "browser.", "openfga."))
        and not issue.code.startswith("database.maintenance_")
    )
    if issues:
        raise ProductionSecurityError(issues)
