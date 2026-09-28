from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vibecanvas_api.services.sandbox import manager as manager_module
from vibecanvas_api.services.sandbox.manager import SandboxSession


class _FakeProcess:
    def poll(self):
        return None


class _ExitedProcess:
    def poll(self):
        return 1


class _NeverConnectedBroker:
    async def wait_connected(self) -> None:
        await asyncio.Event().wait()


class _FakeProvider:
    def __init__(self) -> None:
        self.launches = 0
        self.stops = 0
        self.launch_kwargs: list[dict] = []
        self.lifecycle: list[str] = []

    def launch_agent_runtime_bus(self, **kwargs):
        self.launches += 1
        self.launch_kwargs.append(kwargs)
        return SimpleNamespace(proc=_FakeProcess())

    def stop_run(self, _handle, *, kill: bool = False):
        assert kill is False
        self.stops += 1
        self.lifecycle.append("stop")


@pytest.mark.asyncio
async def test_runtime_restore_exit_fails_before_bus_timeout() -> None:
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="agent_runtime_baseline_restore_exited"):
        await SandboxSession._wait_for_agent_runtime_connection(
            _NeverConnectedBroker(),
            SimpleNamespace(proc=_ExitedProcess()),
            timeout=30.0,
            restored_from_baseline=True,
        )

    assert time.monotonic() - started < 0.5


class _FakeBroker:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.turn_id = ""
        self.closed = False
        self.incoming = asyncio.Queue()
        self.sent = []

    async def start(self) -> None:
        return None

    async def wait_connected(self) -> None:
        return None

    def is_connected(self) -> bool:
        return not self.closed

    async def send(self, message: dict) -> None:
        self.sent.append(message)
        self.turn_id = message["turn_id"]
        if message["type"] == "runtime_request":
            for frame in self.frames(self.turn_id):
                self.incoming.put_nowait({**frame, "turn_id": self.turn_id})
        elif (message.get("response") or {}).get("action") == "cancel":
            self.incoming.put_nowait({"type": "runtime_result", "turn_id": self.turn_id})

    def frames(self, turn_id):
        return [{"type": "runtime_event", "event": {"type": "runtime.started", "turn_id": turn_id}},
                {"type": "runtime_result"}]

    async def messages(self):
        while True:
            yield await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


def _runtime_request(
    turn_id: str,
    *,
    runtime_type: str = "codex",
    model: dict | None = None,
) -> dict:
    """Build the same complete protocol-v2 Turn envelope used in production."""
    resolved_model = dict(model or {})
    if runtime_type == "codex":
        resolved_model.setdefault("id", "gpt-codex-current")
        if resolved_model.get("connection_type") != "chatgpt_account":
            resolved_model.setdefault(
                "base_url",
                "http://platform.test/api/runtime-model/v1",
            )
            resolved_model.setdefault("api_key", "turn-capability")
    return {
        "tenant_id": "tenant",
        "user_id": "user",
        "chat_id": "chat",
        "turn_id": turn_id,
        "runtime_type": runtime_type,
        "runtime_session_id": "runtime-session",
        "runtime_root": f"/runtime/.{runtime_type}",
        "message": {"role": "user", "content": "continue"},
        "model": resolved_model,
    }


class _ResetOnSecondSendBroker(_FakeBroker):
    instances: list["_ResetOnSecondSendBroker"] = []

    def __init__(self, socket_path: str) -> None:
        super().__init__(socket_path)
        self.send_count = 0
        self.__class__.instances.append(self)

    async def send(self, message: dict) -> None:
        self.send_count += 1
        if len(self.__class__.instances) == 1 and self.send_count == 2:
            raise ConnectionResetError("Connection lost")
        await super().send(message)


class _BlockingResultBroker(_FakeBroker):
    def frames(self, turn_id):
        return [{"type": "runtime_event", "event": {"type": "runtime.started", "turn_id": turn_id}}]


class _RuntimeErrorBroker(_FakeBroker):
    def frames(self, turn_id):
        return [{"type": "runtime_event", "event": {"type": "checkpoint", "turn_id": turn_id}},
                {"type": "runtime_error", "error": {"message": "transient model egress failure"}}]


def test_codex_account_auth_is_staged_without_nested_mount_and_reconciled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(manager_module, "resolve_codex_executable", lambda: "/bin/true")
    monkeypatch.setattr(manager_module, "codex_cli_readonly_root", lambda _path: "/bin")
    monkeypatch.setattr(manager_module, "codex_cli_node_runtime", lambda _path: None)
    account_auth = tmp_path / "account" / "auth.json"
    account_auth.parent.mkdir()
    account_auth.write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=_FakeProvider(),
        base_binds=[],
        runtime_dir=str(runtime_dir),
        account_auth_file=str(account_auth),
        expose_run=False,
    )

    rw_binds, _ro_binds, _env = session._agent_runtime_launch_spec(
        runtime_type="codex",
        uses_codex_account=True,
    )

    assert dict(rw_binds) == {"/runtime": str(runtime_dir)}
    staged = runtime_dir / ".codex" / "auth.json"
    assert staged.read_text(encoding="utf-8") == '{"auth_mode":"chatgpt"}'
    staged.write_text(
        '{"auth_mode":"chatgpt","refreshed":true}',
        encoding="utf-8",
    )
    session._persist_codex_account_auth()
    assert account_auth.read_text(encoding="utf-8") == (
        '{"auth_mode":"chatgpt","refreshed":true}'
    )


@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_main_runtime_process_is_reused_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _FakeBroker)
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        expose_run=False,
    )

    first = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request("turn-1")
        )
    ]
    second = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request("turn-2")
        )
    ]

    assert first[0]["turn_id"] == "turn-1"
    assert second[0]["turn_id"] == "turn-2"
    assert provider.launches == 1
    assert provider.stops == 0
    assert provider.launch_kwargs[0]["env_overrides"][
        "AGENT_DEBUG_VIEW_ENABLED"
    ] in {"0", "1"}
    assert "allow_hosts" not in provider.launch_kwargs[0]
    await session.close()
    assert provider.stops == 1


@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_stale_runtime_transport_is_restored_before_user_turn_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    _ResetOnSecondSendBroker.instances = []
    monkeypatch.setattr(manager_module, "BusBroker", _ResetOnSecondSendBroker)
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        expose_run=False,
    )

    first = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request("turn-1")
        )
    ]
    second = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request("turn-2")
        )
    ]

    assert first[0]["turn_id"] == "turn-1"
    assert second[0]["turn_id"] == "turn-2"
    assert provider.launches == 2
    assert provider.stops == 1
    assert len(_ResetOnSecondSendBroker.instances) == 2
    await session.close()


@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_cancelled_turn_keeps_project_transport_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _BlockingResultBroker)
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        expose_run=False,
    )

    stream = session.run_agent_runtime_stream(
        _runtime_request("turn-cancelled")
    )
    assert (await anext(stream))["turn_id"] == "turn-cancelled"

    await stream.aclose()

    assert provider.launches == 1
    assert provider.stops == 0
    assert session._runtime_handle is not None
    assert session._runtime_broker is not None

    await session.close()


@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_project_siblings_run_concurrently_and_cancel_independently(monkeypatch) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _BlockingResultBroker)
    session = SandboxSession(
        tenant_id="tenant", wf_id="project", run_dir=None, overlay_dir=None,
        provider=provider, base_binds=[], expose_run=False,
    )
    first = session.run_agent_runtime_stream(_runtime_request("chat-a-turn"))
    second = session.run_agent_runtime_stream(_runtime_request("chat-b-turn"))
    assert (await anext(first))["turn_id"] == "chat-a-turn"
    event = await asyncio.wait_for(anext(second), timeout=1)
    assert event["turn_id"] == "chat-b-turn"
    broker = session._runtime_broker
    await first.aclose()
    assert provider.stops == 0
    assert provider.launches == 1
    assert "chat-b-turn" in session._runtime_brokers
    # A's late terminal frame must not finish B's stream.
    pending = asyncio.create_task(anext(second))
    await asyncio.sleep(0.02)
    assert not pending.done()
    broker.incoming.put_nowait({"type": "runtime_event", "turn_id": "chat-b-turn",
                               "event": {"type": "message.delta", "turn_id": "chat-b-turn"}})
    assert (await asyncio.wait_for(pending, 1))["turn_id"] == "chat-b-turn"
    assert broker.sent[-1]["response"]["turn_id"] == "chat-a-turn"
    await second.aclose()
    await session.close()



@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_runtime_is_reused_when_external_destination_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _FakeBroker)
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        expose_run=False,
    )

    first = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request(
                "turn-1",
                model={"base_url": "https://model-a.example/v1"},
            )
        )
    ]
    second = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request(
                "turn-2",
                model={"base_url": "https://model-b.example/v1"},
            )
        )
    ]

    assert first[0]["turn_id"] == "turn-1"
    assert second[0]["turn_id"] == "turn-2"
    assert provider.launches == 1
    assert provider.stops == 0
    assert "allow_hosts" not in provider.launch_kwargs[0]
    await session.close()
    assert provider.stops == 1


@pytest.mark.asyncio
async def test_codex_turn_uses_direct_runtime_volume_without_checkpointing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _FakeBroker)
    monkeypatch.setattr(
        manager_module,
        "resolve_codex_executable",
        lambda: "/bin/true",
    )
    monkeypatch.setattr(
        manager_module,
        "codex_cli_readonly_root",
        lambda _executable: "/bin",
    )
    monkeypatch.setattr(
        manager_module,
        "codex_cli_node_runtime",
        lambda _executable: None,
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat-scope",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        runtime_dir=str(runtime_dir),
        skills_dir=str(skills_dir),
        expose_run=False,
    )

    events = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request(
                "turn-1",
                runtime_type="codex",
                model={"connection_type": "managed_api"},
            )
        )
    ]

    assert events == [{"type": "runtime.started", "turn_id": "turn-1"}]
    runtime_binds = dict(provider.launch_kwargs[0]["extra_rw_binds"])
    assert runtime_binds["/runtime"] == str(runtime_dir)
    assert "/runtime/.codex/auth.json" not in runtime_binds
    assert (runtime_dir / ".codex" / "auth.json").read_text(encoding="utf-8") == ""
    marker = runtime_dir / "context.txt"
    marker.write_text("durable without serialization", encoding="utf-8")
    await session.close()
    assert marker.read_text(encoding="utf-8") == "durable without serialization"
    assert provider.lifecycle == ["stop"]


@pytest.mark.asyncio
async def test_failed_turn_syncs_volume_without_stopping_sibling_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _RuntimeErrorBroker)
    monkeypatch.setattr(manager_module, "resolve_codex_executable", lambda: "/bin/true")
    monkeypatch.setattr(manager_module, "codex_cli_readonly_root", lambda _path: "/bin")
    monkeypatch.setattr(manager_module, "codex_cli_node_runtime", lambda _path: None)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_volume = SimpleNamespace(
        volume_id="runtime-volume",
        path=str(runtime_dir),
        storage_prefix="project-runtime-v1/test",
    )
    persisted: list[str] = []

    class RuntimeVolumeProvider:
        def sync(self, volume):
            assert provider.lifecycle == []
            persisted.append(volume.volume_id)
            return 1

    monkeypatch.setattr(
        manager_module,
        "get_project_runtime_volume_provider",
        lambda: RuntimeVolumeProvider(),
    )
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat-scope",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        runtime_dir=str(runtime_dir),
        runtime_volume=runtime_volume,
        expose_run=False,
    )

    with pytest.raises(RuntimeError, match="transient model egress failure"):
        _ = [
            event
            async for event in session.run_agent_runtime_stream(
                {
                    "tenant_id": "tenant",
                    "user_id": "user",
                    "chat_id": "chat",
                    "turn_id": "turn-1",
                    "runtime_type": "codex",
                    "runtime_session_id": "runtime-session",
                    "runtime_root": "/runtime/.codex",
                    "message": {"role": "user", "content": "continue"},
                    "model": {
                        "id": "gpt-codex-current",
                        "connection_type": "managed_api",
                        "base_url": "http://platform.test/api/runtime-model/v1",
                        "api_key": "turn-capability",
                    },
                }
            )
        ]

    assert persisted == ["runtime-volume"]
    await session.close()


@pytest.mark.asyncio
async def test_codex_account_runtime_is_reused_until_session_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _FakeBroker)
    monkeypatch.setattr(manager_module, "resolve_codex_executable", lambda: "/bin/true")
    monkeypatch.setattr(
        manager_module,
        "codex_cli_readonly_root",
        lambda _executable: "/bin",
    )
    monkeypatch.setattr(manager_module, "codex_cli_node_runtime", lambda _executable: None)
    auth_file = tmp_path / "account" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat-scope",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        runtime_dir=str(runtime_dir),
        account_auth_file=str(auth_file),
        expose_run=False,
    )

    first = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request(
                "turn-1",
                runtime_type="codex",
                model={"connection_type": "chatgpt_account"},
            )
        )
    ]
    second = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request(
                "turn-2",
                runtime_type="codex",
                model={"connection_type": "chatgpt_account"},
            )
        )
    ]

    assert first[0]["turn_id"] == "turn-1"
    assert second[0]["turn_id"] == "turn-2"
    assert provider.launches == 1
    assert provider.stops == 0
    assert session._runtime_uses_codex_account is True
    assert session.resource_status()["authentication"] == "account_bound"
    runtime_binds = dict(provider.launch_kwargs[0]["extra_rw_binds"])
    assert "/runtime/.codex/auth.json" not in runtime_binds
    assert (runtime_dir / ".codex" / "auth.json").read_text(
        encoding="utf-8"
    ) == '{"auth_mode":"chatgpt"}'

    (runtime_dir / ".codex" / "auth.json").write_text(
        '{"auth_mode":"chatgpt","refreshed":true}',
        encoding="utf-8",
    )
    await session.close()
    assert provider.stops == 1
    assert auth_file.read_text(encoding="utf-8") == (
        '{"auth_mode":"chatgpt","refreshed":true}'
    )


def test_codex_runtime_mounts_resolved_browser_cli_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    provider = _FakeProvider()
    package_root = tmp_path / "browser-runtime"
    package_root.mkdir()
    launcher = package_root / "browser-runtime.cjs"
    launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    launcher.chmod(0o755)
    shim_root = tmp_path / "bin"
    shim_root.mkdir()
    shim = shim_root / "flowork-browser-runtime"
    shim.symlink_to(launcher)

    monkeypatch.setattr(manager_module, "resolve_codex_executable", lambda: "/bin/true")
    monkeypatch.setattr(
        manager_module,
        "codex_cli_readonly_root",
        lambda _executable: "/bin",
    )
    monkeypatch.setattr(manager_module, "codex_cli_node_runtime", lambda _path: None)
    monkeypatch.setenv("BROWSER_CLI_COMMAND", str(shim))
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        expose_run=False,
    )

    _rw_binds, ro_binds, _env = session._agent_runtime_launch_spec(
        runtime_type="codex",
        uses_codex_account=False,
    )

    assert str(package_root) in ro_binds


def test_codex_runtime_mounts_diagram_search_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    provider = _FakeProvider()
    package_root = tmp_path / "drawio-mcp"
    package_root.mkdir()
    launcher = package_root / "search.mjs"
    launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    launcher.chmod(0o755)
    shim_root = tmp_path / "bin"
    shim_root.mkdir()
    shim = shim_root / "flowork-diagram-search"
    shim.symlink_to(launcher)
    desktop_root = tmp_path / "drawio-desktop"
    desktop_root.mkdir()
    desktop = desktop_root / "drawio"
    desktop.write_text("#!/bin/sh\n", encoding="utf-8")
    desktop.chmod(0o755)
    desktop_shim = shim_root / "drawio"
    desktop_shim.symlink_to(desktop)

    monkeypatch.setattr(manager_module, "resolve_codex_executable", lambda: "/bin/true")
    monkeypatch.setattr(
        manager_module,
        "codex_cli_readonly_root",
        lambda _executable: "/bin",
    )
    monkeypatch.setattr(manager_module, "codex_cli_node_runtime", lambda _path: None)
    monkeypatch.setenv("BROWSER_CLI_COMMAND", str(tmp_path / "missing"))
    monkeypatch.setenv("DIAGRAM_SEARCH_COMMAND", str(shim))
    monkeypatch.setenv("DRAWIO_CLI_COMMAND", str(desktop_shim))
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        expose_run=False,
    )

    _rw_binds, ro_binds, _env = session._agent_runtime_launch_spec(
        runtime_type="codex",
        uses_codex_account=False,
    )

    assert str(package_root) in ro_binds
    assert str(desktop_root) in ro_binds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_type", "model", "expected_auth"),
    [
        ("codex", {"connection_type": "managed_api"}, "detached"),
        ("codex", {"connection_type": "chatgpt_account"}, "account_bound"),
    ],
)
async def test_all_interactive_runtimes_share_hibernate_security_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    runtime_type: str,
    model: dict,
    expected_auth: str,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _FakeBroker)
    monkeypatch.setattr(manager_module.config, "sandbox_resident_mode", "snapshot")
    monkeypatch.setattr(manager_module, "resolve_codex_executable", lambda: "/bin/true")
    monkeypatch.setattr(manager_module, "codex_cli_readonly_root", lambda _path: "/bin")
    monkeypatch.setattr(manager_module, "codex_cli_node_runtime", lambda _path: None)
    auth_file = tmp_path / "account" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text("{}", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    session = SandboxSession(
        tenant_id="tenant",
        wf_id=f"chat-{runtime_type}-{model.get('connection_type', 'api')}",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        account_auth_file=str(auth_file),
        runtime_dir=str(runtime_dir),
        expose_run=False,
    )
    session.writeback_vfs = AsyncMock()

    events = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request(
                "turn-1",
                runtime_type=runtime_type,
                model=model,
            )
        )
    ]
    assert events[0]["turn_id"] == "turn-1"
    assert session.resource_status()["authentication"] == expected_auth

    assert await session.hibernate() is True
    resources = session.resource_status()
    assert session._lifecycle_state == "hibernated"
    assert resources["runtime_type"] == runtime_type
    assert resources["runtime_process"] == "stopped"
    assert resources["authentication"] == "detached"
    assert resources["network"] == "disconnected"
    assert provider.stops == 1

    await session.close()


@pytest.mark.asyncio
async def test_codex_runtime_rejects_account_and_broker_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(manager_module, "BusBroker", _FakeBroker)
    monkeypatch.setattr(
        manager_module,
        "resolve_codex_executable",
        lambda: "/bin/true",
    )
    monkeypatch.setattr(
        manager_module,
        "codex_cli_readonly_root",
        lambda _path: "/bin",
    )
    monkeypatch.setattr(
        manager_module,
        "codex_cli_node_runtime",
        lambda _path: None,
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    account_auth = tmp_path / "account" / "auth.json"
    account_auth.parent.mkdir()
    account_auth.write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    session = SandboxSession(
        tenant_id="tenant",
        wf_id="chat",
        run_dir=None,
        overlay_dir=None,
        provider=provider,
        base_binds=[],
        runtime_dir=str(runtime_dir),
        account_auth_file=str(account_auth),
        expose_run=False,
    )

    account_events = [
        event
        async for event in session.run_agent_runtime_stream(
            _runtime_request(
                "turn-account",
                runtime_type="codex",
                model={"connection_type": "chatgpt_account"},
            )
        )
    ]
    with pytest.raises(RuntimeError, match="sandbox_runtime_binding_mismatch"):
        _ = [
            event
            async for event in session.run_agent_runtime_stream(
                _runtime_request("turn-broker", runtime_type="codex")
            )
        ]

    assert account_events[0]["turn_id"] == "turn-account"
    assert provider.launches == 1
    # A rejected binding cannot tear down a sibling Chat's valid transport.
    assert provider.stops == 0
    assert session._bound_runtime_type == "codex"
    assert session._bound_runtime_uses_codex_account is True
    assert (runtime_dir / ".codex" / "auth.json").read_text(
        encoding="utf-8"
    ) == '{"auth_mode":"chatgpt"}'
    await session.close()
