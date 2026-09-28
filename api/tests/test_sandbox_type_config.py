from __future__ import annotations

import pytest

from vibecanvas_api.config import AppConfig


_SANDBOX_ENV = (
    "SANDBOX_TYPE",
    "SANDBOX_GVISOR_PLATFORM",
    "SANDBOX_IDLE_TTL_S",
    "SANDBOX_WARM_IDLE_TTL_S",
    "SANDBOX_SNAPSHOT_IDLE_TTL_S",
    "SANDBOX_WORKFLOW_SNAPSHOT_TTL_S",
    "SANDBOX_ACTIVITY_POLL_INTERVAL_S",
    "SANDBOX_SNAPSHOT_CHECKPOINT_TIMEOUT_S",
    "SANDBOX_SNAPSHOT_RESTORE_TIMEOUT_S",
    "SANDBOX_SNAPSHOT_COMPRESSION",
    "SANDBOX_SNAPSHOT_MAX_COUNT",
    "SANDBOX_SNAPSHOT_MAX_BYTES",
    "SANDBOX_MAX_MOUNTS",
    "SANDBOX_MAX_RESIDENT",
)


@pytest.fixture(autouse=True)
def _clean_sandbox_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SANDBOX_ENV:
        monkeypatch.delenv(name, raising=False)


def test_rootful_snapshot_profile_has_two_interactive_session_ttls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_TYPE", "rootful-snapshot")
    configured = AppConfig({})

    assert configured.sandbox_rootful is True
    assert configured.sandbox_resident_mode == "snapshot"
    assert configured.sandbox_warm_idle_ttl_s == 300
    assert configured.sandbox_snapshot_idle_ttl_s == 1800
    assert configured.sandbox_workflow_snapshot_ttl_s == 86400
    assert configured.sandbox_activity_poll_interval_s == 5.0


def test_snapshot_ttls_are_independent_stage_durations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_TYPE", "rootful-snapshot")
    monkeypatch.setenv("SANDBOX_WARM_IDLE_TTL_S", "45")
    monkeypatch.setenv("SANDBOX_SNAPSHOT_IDLE_TTL_S", "10")
    configured = AppConfig({})

    assert configured.sandbox_warm_idle_ttl_s == 45
    assert configured.sandbox_snapshot_idle_ttl_s == 10


def test_sandbox_resident_capacity_defaults_to_two_and_is_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert AppConfig({}).sandbox_max_resident == 2

    monkeypatch.setenv("SANDBOX_MAX_RESIDENT", "4")
    assert AppConfig({}).sandbox_max_resident == 4


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SANDBOX_WARM_IDLE_TTL_S", "0"),
        ("SANDBOX_SNAPSHOT_IDLE_TTL_S", "-1"),
        ("SANDBOX_WORKFLOW_SNAPSHOT_TTL_S", "0"),
        ("SANDBOX_ACTIVITY_POLL_INTERVAL_S", "0.1"),
        ("SANDBOX_SNAPSHOT_CHECKPOINT_TIMEOUT_S", "0.5"),
        ("SANDBOX_SNAPSHOT_RESTORE_TIMEOUT_S", "901"),
        ("SANDBOX_SNAPSHOT_MAX_COUNT", "0"),
        ("SANDBOX_SNAPSHOT_MAX_BYTES", "1024"),
        ("SANDBOX_MAX_MOUNTS", "7"),
    ],
)
def test_invalid_snapshot_operational_limits_fail_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        AppConfig({})


def test_unknown_sandbox_type_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_TYPE", "auto")
    with pytest.raises(ValueError, match="SANDBOX_TYPE must be one of"):
        AppConfig({})


def test_gvisor_platform_is_explicit_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert AppConfig({}).sandbox_gvisor_platform == "systrap"

    monkeypatch.setenv("SANDBOX_GVISOR_PLATFORM", "ptrace")
    assert AppConfig({}).sandbox_gvisor_platform == "ptrace"

    monkeypatch.setenv("SANDBOX_GVISOR_PLATFORM", "auto")
    with pytest.raises(ValueError, match="SANDBOX_GVISOR_PLATFORM"):
        AppConfig({})


def test_legacy_idle_ttl_cannot_ambiguously_override_snapshot_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_TYPE", "rootful-snapshot")
    monkeypatch.setenv("SANDBOX_IDLE_TTL_S", "60")
    with pytest.raises(ValueError, match="SANDBOX_IDLE_TTL_S conflicts"):
        AppConfig({})


def test_snapshot_compression_is_explicitly_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SNAPSHOT_COMPRESSION", "zstd")
    with pytest.raises(ValueError, match="SANDBOX_SNAPSHOT_COMPRESSION"):
        AppConfig({})
