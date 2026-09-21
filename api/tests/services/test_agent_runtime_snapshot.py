from __future__ import annotations

from types import SimpleNamespace

from vibecanvas_api.services.sandbox import agent_runtime_snapshot


def test_source_fingerprint_covers_aggregate_mcp_hub_runtime() -> None:
    hashes = agent_runtime_snapshot._source_hashes()

    assert "services/agent_runtime/codex_mcp_hub_gateway.py" in hashes
    assert "services/agent_runtime/mcp_hub.py" in hashes
    assert "services/agent_runtime/mcp_hub_adapter.py" in hashes
    assert "services/agent_runtime/mcp_browser_transport.py" in hashes
    assert "services/agent_runtime/mcp_runtime_protocol.py" in hashes


def test_fingerprint_ignores_substitutable_runtime_resource_sources(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        agent_runtime_snapshot.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "runsc version test",
    )
    monkeypatch.setattr(
        agent_runtime_snapshot,
        "_source_hashes",
        lambda: {"sandbox_entry.py": "same"},
    )
    provider = SimpleNamespace(_runsc="/usr/bin/runsc")

    first = agent_runtime_snapshot._fingerprint(
        provider,
        runtime_type="codex",
        rw_binds=[("/data", "/tmp/chat-a/data")],
        ro_binds=[("/skills", "/runtime/user-a/skills"), "/usr"],
        env_overrides={"VC_AGENT_RUNTIME_TYPE": "codex"},
    )
    second = agent_runtime_snapshot._fingerprint(
        provider,
        runtime_type="codex",
        rw_binds=[("/data", "/tmp/chat-b/data")],
        ro_binds=[("/skills", "/runtime/user-b/skills"), "/usr"],
        env_overrides={"VC_AGENT_RUNTIME_TYPE": "codex"},
    )

    assert first == second


def test_fingerprint_keeps_host_identity_code_mounts(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_runtime_snapshot.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "runsc version test",
    )
    monkeypatch.setattr(agent_runtime_snapshot, "_source_hashes", lambda: {})
    provider = SimpleNamespace(_runsc="/usr/bin/runsc")

    first = agent_runtime_snapshot._fingerprint(
        provider,
        runtime_type="codex",
        rw_binds=[],
        ro_binds=["/opt/codex-v1"],
        env_overrides={},
    )
    second = agent_runtime_snapshot._fingerprint(
        provider,
        runtime_type="codex",
        rw_binds=[],
        ro_binds=["/opt/codex-v2"],
        env_overrides={},
    )

    assert first != second
