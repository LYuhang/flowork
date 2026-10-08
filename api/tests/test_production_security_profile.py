from __future__ import annotations

from vibecanvas_api.config import AppConfig
from vibecanvas_api.security_profile import (
    ProductionSecurityError,
    production_security_issues,
    validate_production_security,
)


def _config(tmp_path, monkeypatch, **env) -> AppConfig:
    names = {
        "VIBECANVAS_ENV",
        "VIBECANVAS_PUBLIC_URL",
        "DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "ADMIN_DATABASE_URL",
        "REDIS_URL",
        "VIBECANVAS_SIGNING_SECRET",
        "BROWSER_TOKEN_SECRET",
        "OPENFGA_API_URL",
        "OPENFGA_STORE_ID",
        "OPENFGA_AUTHORIZATION_MODEL_ID",
        "OPENFGA_API_TOKEN",
        "KMS_PROVIDER",
        "KMS_KEY_ID",
        "KMS_WORKLOAD_IDENTITY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "CONTENT_LOOKUP_HMAC_KEY",
        "CONTENT_LOOKUP_HMAC_KEY_FILE",
        "OAUTH_ENCRYPTION_KEY",
        "SMTP_HOST",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_PASSWORD_SECRET_ID",
        "OBJECT_STORE_PROVIDER",
        "S3_BUCKET",
        "S3_SERVER_SIDE_ENCRYPTION",
        "S3_KMS_KEY_ID",
        "AUDIT_EXPORT_URL",
        "BACKUP_ENCRYPTION_VERIFIED",
        "PURGE_WORKER_ENABLED",
        "DISTRIBUTED_AUTH_RATE_LIMIT_ENABLED",
        "HIGH_RISK_STEP_UP_REQUIRED",
        "WEBAUTHN_RP_ID",
        "WEBAUTHN_ORIGIN",
        "WEBAUTHN_RP_NAME",
        "WEB_SESSION_COOKIE_ENABLED",
        "EXTENSION_SCOPED_TOKEN_ENABLED",
        "SANDBOX_EGRESS_MODE",
        "SANDBOX_NETWORK",
        "ENABLE_TEST_USER",
        "AGENT_DEBUG_VIEW_ENABLED",
        "SANDBOX_DEBUG_EXECUTE_ENABLED",
        "VIBECANVAS_FRAME_ANCESTORS",
        "TRUSTED_PROXY_CIDRS",
        "UPLOAD_SCANNER_PROVIDER",
        "UPLOAD_SCANNER_CLAMD_UNIX_SOCKET",
        "UPLOAD_SCANNER_TIMEOUT_SECONDS",
        "RUN_DATABASE_MIGRATIONS",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return AppConfig({"storage": {"root": str(tmp_path / "storage")}})


def test_development_profile_does_not_require_production_services(
    tmp_path, monkeypatch,
) -> None:
    cfg = _config(tmp_path, monkeypatch, VIBECANVAS_ENV="development")
    assert production_security_issues(
        cfg,
        cors_origins=["http://localhost:3000"],
    ) == ()


def test_production_profile_reports_all_insecure_fallbacks_without_values(
    tmp_path, monkeypatch,
) -> None:
    cfg = _config(
        tmp_path,
        monkeypatch,
        VIBECANVAS_ENV="production",
        ENABLE_TEST_USER="1",
        AGENT_DEBUG_VIEW_ENABLED="1",
        SANDBOX_DEBUG_EXECUTE_ENABLED="1",
    )
    issues = production_security_issues(
        cfg,
        cors_origins=["*", "http://localhost:3000"],
    )
    codes = {issue.code for issue in issues}
    assert {
        "public_url.public_https_required",
        "database.tls_verification_required",
        "redis.tls_required",
        "signing.stable_secret_required",
        "openfga.store_id_required",
        "kms.workload_identity_required",
        "session.secure_cookie_required",
        "browser.scoped_exchange_required",
        "upload.malware_scanner_required",
        "upload.scanner_unix_socket_required",
        "proxy.trusted_cidrs_required",
        "debug.test_user_forbidden",
        "debug.inspector_forbidden",
        "debug.sandbox_execute_forbidden",
        "cors.wildcard",
        "cors.origin_not_public_https",
    } <= codes
    try:
        validate_production_security(
            cfg,
            cors_origins=["https://app.example.com"],
        )
    except ProductionSecurityError as exc:
        rendered = str(exc)
    else:  # pragma: no cover - the fixture is intentionally insecure
        raise AssertionError("production validation unexpectedly succeeded")
    assert "dev:dev" not in rendered
    assert "dev-insecure-browser-secret" not in rendered


def test_production_profile_accepts_exact_https_ip_origins(
    tmp_path, monkeypatch,
) -> None:
    cfg = _config(
        tmp_path,
        monkeypatch,
        VIBECANVAS_ENV="production",
        VIBECANVAS_PUBLIC_URL="https://192.0.2.25:9001",
    )
    codes = {
        issue.code for issue in production_security_issues(
            cfg,
            cors_origins=["https://192.0.2.25:9001", "https://[2001:db8::25]:9001"],
        )
    }
    assert "public_url.public_https_required" not in codes
    assert "cors.origin_not_public_https" not in codes


def test_production_profile_accepts_explicit_secure_configuration(
    tmp_path, monkeypatch,
) -> None:
    cfg = _config(
        tmp_path,
        monkeypatch,
        VIBECANVAS_ENV="production",
        VIBECANVAS_PUBLIC_URL="https://app.example.com",
        DATABASE_URL=(
            "postgresql+asyncpg://vc_app:correct-horse-battery-staple@"
            "db.example.com/vc?sslmode=verify-full"
        ),
        MAINTENANCE_DATABASE_URL=(
            "postgresql+asyncpg://vc_maintenance:another-long-secret@"
            "db.example.com/vc?sslmode=verify-full"
        ),
        REDIS_URL="rediss://:a-long-production-redis-secret@redis.example.com/0",
        VIBECANVAS_SIGNING_SECRET="s" * 48,
        BROWSER_TOKEN_SECRET="b" * 48,
        OPENFGA_API_URL="https://openfga.example.com",
        OPENFGA_STORE_ID="store-1",
        OPENFGA_AUTHORIZATION_MODEL_ID="model-1",
        OPENFGA_API_TOKEN="opaque-openfga-token",
        KMS_PROVIDER="aws-kms",
        KMS_KEY_ID="alias/vibecanvas",
        KMS_WORKLOAD_IDENTITY="arn:aws:iam::123456789012:role/vibecanvas",
        CONTENT_LOOKUP_HMAC_KEY="l" * 48,
        OAUTH_ENCRYPTION_KEY="o" * 44,
        SMTP_HOST="smtp.example.com",
        SMTP_USER="noreply@example.com",
        SMTP_PASSWORD_SECRET_ID="prod/vibecanvas/smtp-password",
        OBJECT_STORE_PROVIDER="s3",
        S3_BUCKET="vibecanvas-private",
        S3_SERVER_SIDE_ENCRYPTION="aws:kms",
        S3_KMS_KEY_ID="alias/vibecanvas-object-store",
        AUDIT_EXPORT_URL="https://audit.example.com/v1/events",
        BACKUP_ENCRYPTION_VERIFIED="1",
        PURGE_WORKER_ENABLED="1",
        DISTRIBUTED_AUTH_RATE_LIMIT_ENABLED="1",
        HIGH_RISK_STEP_UP_REQUIRED="1",
        WEB_SESSION_COOKIE_ENABLED="1",
        EXTENSION_SCOPED_TOKEN_ENABLED="1",
        UPLOAD_SCANNER_PROVIDER="clamd",
        UPLOAD_SCANNER_CLAMD_UNIX_SOCKET="/run/clamav/clamd.sock",
        SANDBOX_EGRESS_MODE="proxy",
        SANDBOX_NETWORK="none",
        VIBECANVAS_FRAME_ANCESTORS="'self' chrome-extension://fixed-extension-id",
        TRUSTED_PROXY_CIDRS="10.0.0.0/8,fd00::/8",
    )
    validate_production_security(
        cfg,
        cors_origins=["https://app.example.com"],
    )


def test_production_profile_rejects_runtime_schema_migrations(
    tmp_path, monkeypatch,
) -> None:
    cfg = _config(
        tmp_path,
        monkeypatch,
        VIBECANVAS_ENV="production",
        RUN_DATABASE_MIGRATIONS="true",
    )
    codes = {
        issue.code
        for issue in production_security_issues(cfg, cors_origins=[])
    }
    assert "database.runtime_migrations_forbidden" in codes


def test_production_profile_rejects_deployment_credentials_in_runtime(
    tmp_path, monkeypatch,
) -> None:
    cfg = _config(
        tmp_path,
        monkeypatch,
        VIBECANVAS_ENV="production",
        MIGRATION_DATABASE_URL=(
            "postgresql+asyncpg://migrator:secret@db.example/vc"
        ),
        ADMIN_DATABASE_URL=(
            "postgresql+asyncpg://postgres:secret@db.example/vc"
        ),
    )
    codes = {
        issue.code
        for issue in production_security_issues(cfg, cors_origins=[])
    }
    assert "database.migration_dsn_forbidden" in codes
    assert "database.legacy_admin_dsn_forbidden" in codes


def test_production_profile_rejects_unknown_kms_and_static_aws_keys(
    tmp_path, monkeypatch,
) -> None:
    cfg = _config(
        tmp_path,
        monkeypatch,
        VIBECANVAS_ENV="production",
        KMS_PROVIDER="future-kms",
        AWS_ACCESS_KEY_ID="static-key-id",
        AWS_SECRET_ACCESS_KEY="static-key-secret",
    )
    codes = {
        issue.code
        for issue in production_security_issues(cfg, cors_origins=[])
    }
    assert "kms.managed_provider_required" in codes
    assert "kms.static_aws_credentials_forbidden" in codes


def test_browser_gateway_requires_identity_services_without_file_or_maintenance_access(tmp_path, monkeypatch):
    import pytest
    from vibecanvas_api.security_profile import validate_browser_gateway_security

    cfg = _config(
        tmp_path, monkeypatch, VIBECANVAS_ENV="production",
        DATABASE_URL="postgresql+asyncpg://vc_app:correct-horse-battery-staple@db.example.com/vc?sslmode=verify-full",
        REDIS_URL="rediss://:a-long-production-redis-secret@redis.example.com/0",
        VIBECANVAS_SIGNING_SECRET="s" * 48, BROWSER_TOKEN_SECRET="b" * 48,
        OPENFGA_API_URL="https://openfga.example.com", OPENFGA_STORE_ID="store-1",
        OPENFGA_AUTHORIZATION_MODEL_ID="model-1", OPENFGA_API_TOKEN="opaque-token",
        EXTENSION_SCOPED_TOKEN_ENABLED="1", RUN_DATABASE_MIGRATIONS="0",
    )
    # No maintenance credential, object store, KMS or scanner is configured.
    validate_browser_gateway_security(cfg)
    cfg.browser_token_secret = "dev-insecure-browser-secret-change-me"
    with pytest.raises(ProductionSecurityError) as error:
        validate_browser_gateway_security(cfg)
    assert {issue.code for issue in error.value.issues} == {"browser.stable_token_secret_required"}
