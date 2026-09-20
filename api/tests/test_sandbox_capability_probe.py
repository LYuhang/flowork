from __future__ import annotations

from pathlib import Path

from vibecanvas_api.services import sandbox
from vibecanvas_api.services.sandbox.provider import SandboxResult


class _ProbeProvider:
    write_marker = True

    def __init__(self, runsc_path: str) -> None:
        self.runsc_path = runsc_path

    def run(
        self,
        *,
        run_dir: str,
        command: list[str],
        env: dict[str, str],
        network: str,
        timeout: float,
        extra_ro_binds: list[str],
        extra_rw_binds: list[tuple[str, str]],
        bus_socket: str,
    ) -> SandboxResult:
        assert self.runsc_path == "/test/runsc"
        assert command[0]
        assert "import vibecanvas_engine" in command[-1]
        assert "/run/.capability-probe" in command[-1]
        assert isinstance(env, dict)
        assert network == "none"
        assert timeout == 15.0
        assert isinstance(extra_ro_binds, list)
        assert [destination for destination, _source in extra_rw_binds] == [
            "/data",
            "/memory",
            "/logs",
            "/mount",
        ]
        assert bus_socket.endswith("/bus/probe.sock")
        if self.write_marker:
            Path(run_dir, ".capability-probe").write_text(
                "flowork-gvisor-ready",
                encoding="utf-8",
            )
        return SandboxResult(exit_code=0, stdout="", stderr="", duration_s=0.01)


def _reset_probe_cache() -> None:
    sandbox._GVISOR_RUNNABLE = None
    sandbox._GVISOR_RUNNABLE_PROFILE = None


def test_gvisor_capability_probe_requires_real_bind_roundtrip(monkeypatch) -> None:
    from vibecanvas_api.config import config

    class SuccessfulProbe(_ProbeProvider):
        write_marker = True

    monkeypatch.setattr(sandbox, "_resolve_runsc", lambda: "/test/runsc")
    monkeypatch.setattr(sandbox, "RootlessGvisorProvider", SuccessfulProbe)
    monkeypatch.setattr(config, "sandbox_rootful", False)
    monkeypatch.setattr(config, "sandbox_gvisor_platform", "ptrace")
    _reset_probe_cache()
    try:
        assert sandbox._gvisor_runnable() is True
    finally:
        _reset_probe_cache()


def test_gvisor_capability_probe_rejects_boot_without_bind_roundtrip(monkeypatch) -> None:
    from vibecanvas_api.config import config

    class MissingMarkerProbe(_ProbeProvider):
        write_marker = False

    monkeypatch.setattr(sandbox, "_resolve_runsc", lambda: "/test/runsc")
    monkeypatch.setattr(sandbox, "RootlessGvisorProvider", MissingMarkerProbe)
    monkeypatch.setattr(config, "sandbox_rootful", False)
    monkeypatch.setattr(config, "sandbox_gvisor_platform", "ptrace")
    _reset_probe_cache()
    try:
        assert sandbox._gvisor_runnable() is False
    finally:
        _reset_probe_cache()
