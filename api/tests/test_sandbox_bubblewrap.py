from __future__ import annotations

import os
import signal
import subprocess

import pytest

from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
from vibecanvas_api.services.sandbox.gvisor import BusRunHandle, ServeHandle, ServeSnapshot
from vibecanvas_api.services.sandbox.provider import SandboxResult


def _provider() -> BubblewrapProvider:
    return BubblewrapProvider("/usr/bin/bwrap")


def test_build_argv_binds_run_dir_and_extra_binds(tmp_path):
    run_dir = str(tmp_path / "rundir")
    mount_dir = str(tmp_path / "mount")
    os.makedirs(run_dir)
    os.makedirs(mount_dir)
    provider = _provider()

    argv = provider._build_argv(
        command=["true"],
        env={"A": "1"},
        network=None,
        rw_binds=[("/run", run_dir), ("/mount", mount_dir)],
        extra_ro_binds=(),
        ro_dest_binds=[],
    )

    assert argv[0] == "/usr/bin/bwrap"
    assert "--bind" in argv
    assert argv[argv.index("--bind") + 1 : argv.index("--bind") + 3] == [
        run_dir,
        "/run",
    ]
    assert mount_dir in argv and "/mount" in argv
    assert argv.count("--tmpfs") >= 2  # root "/" plus "/tmp" (not shadowed here)
    assert "/tmp" in argv
    # /proc is bind-mounted from the host, not freshly mounted — see
    # _build_argv's comment on why (EPERM on this host's nested mount ns).
    proc_bind_index = argv.index("/proc")
    assert argv[proc_bind_index - 1] == "--ro-bind"
    assert argv[proc_bind_index + 1] == "/proc"
    assert "--dev" in argv and "/dev" in argv
    assert argv[-3:] == ["--", "true"] or argv[-2:] == ["--", "true"]
    assert "--" in argv and argv[argv.index("--") + 1 :] == ["true"]
    assert "--clearenv" in argv
    setenv_index = argv.index("--setenv")
    assert argv[setenv_index + 1 : setenv_index + 3] == ["A", "1"]
    chdir_index = argv.index("--chdir")
    assert argv[chdir_index + 1] == "/run"


def test_build_argv_skips_tmp_tmpfs_when_channel_at_tmp(tmp_path):
    """A pure Chat code job binds its narrow job channel at /tmp (see

    ``RootlessGvisorProvider.run``'s ``run_mount`` parameter). That bind must
    replace the default ``/tmp`` tmpfs, not duplicate it.
    """
    channel_dir = str(tmp_path / "channel")
    os.makedirs(channel_dir)
    provider = _provider()

    argv = provider._build_argv(
        command=["true"],
        env={},
        network=None,
        rw_binds=[("/tmp", channel_dir)],
        extra_ro_binds=(),
        ro_dest_binds=[],
    )

    tmpfs_indices = [i for i, arg in enumerate(argv) if arg == "--tmpfs"]
    tmpfs_targets = [argv[i + 1] for i in tmpfs_indices]
    assert tmpfs_targets == ["/"]  # only the empty root, not a second /tmp
    bind_index = argv.index("--bind")
    assert argv[bind_index + 1 : bind_index + 3] == [channel_dir, "/tmp"]


def test_build_argv_network_none_omits_share_net(tmp_path):
    provider = _provider()
    argv = provider._build_argv(
        command=["true"], env={}, network="none",
        rw_binds=[("/run", str(tmp_path))], extra_ro_binds=(), ro_dest_binds=[],
    )
    assert "--share-net" not in argv


@pytest.mark.parametrize("channel", ["/run", "/tmp"])
def test_readonly_runtime_is_mounted_after_tmp_parent(tmp_path, channel):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    argv = _provider()._build_argv(
        command=["true"], env={}, network="none",
        rw_binds=[(channel, str(tmp_path))],
        extra_ro_binds=(), ro_dest_binds=[("/tmp/runtime", str(runtime))],
    )
    child_index = argv.index("/tmp/runtime")
    assert argv[child_index - 2:child_index + 1] == ["--ro-bind", str(runtime), "/tmp/runtime"]
    assert argv.index("/tmp") < child_index
    assert argv[argv.index("--chdir") + 1] == channel


def test_nested_writable_mount_order_does_not_change_cwd(tmp_path):
    argv = _provider()._build_argv(
        command=["true"], env={}, network="none",
        rw_binds=[("/data/nested", str(tmp_path)), ("/data", str(tmp_path))],
        extra_ro_binds=(), ro_dest_binds=[],
    )
    assert argv.index("/data") < argv.index("/data/nested")
    assert argv[argv.index("--chdir") + 1] == "/data/nested"


def test_build_argv_host_network_includes_share_net(tmp_path):
    provider = _provider()
    argv = provider._build_argv(
        command=["true"], env={}, network="host",
        rw_binds=[("/run", str(tmp_path))], extra_ro_binds=(), ro_dest_binds=[],
    )
    assert "--share-net" in argv


def test_run_returns_sandbox_result_from_subprocess(monkeypatch, tmp_path):
    run_dir = str(tmp_path / "rundir")
    os.makedirs(run_dir)
    captured = {}

    class FakeProcess:
        pid = 4242

        def communicate(self, timeout=None):
            return "stdout-text", "stderr-text"

        returncode = 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    provider = _provider()

    result = provider.run(run_dir=run_dir, command=["true"], timeout=5.0)

    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "stdout-text"
    assert result.stderr == "stderr-text"
    assert captured["argv"][0] == "/usr/bin/bwrap"


def test_run_kills_process_group_on_timeout(monkeypatch, tmp_path):
    run_dir = str(tmp_path / "rundir")
    os.makedirs(run_dir)
    killed = {}

    class FakeProcess:
        pid = 4242
        calls = 0

        def communicate(self, timeout=None):
            FakeProcess.calls += 1
            if FakeProcess.calls == 1:
                raise subprocess.TimeoutExpired(cmd="bwrap", timeout=timeout)
            return "", ""

    def fake_popen(argv, **kwargs):
        return FakeProcess()

    def fake_killpg(pgid, sig):
        killed["pgid"] = pgid
        killed["sig"] = sig

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    provider = _provider()

    result = provider.run(run_dir=run_dir, command=["sleep", "100"], timeout=0.01)

    assert result.exit_code == -signal.SIGKILL
    assert killed["sig"] == signal.SIGKILL


def test_launch_agent_runtime_bus_binds_bus_socket_and_sets_env(monkeypatch, tmp_path):
    from vibecanvas_api.services.sandbox import bubblewrap as bubblewrap_module

    monkeypatch.setattr(bubblewrap_module.config, "sandbox_egress_mode", "host-network")
    overlay_dir = str(tmp_path / "overlay")
    bus_socket = str(tmp_path / "bus" / "run.sock")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeServeProcess(pid=7171)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    provider = _provider()

    handle = provider.launch_agent_runtime_bus(
        run_id="agent-runtime-test",
        bus_socket=bus_socket,
        tenant="tenant-1",
        extra_rw_binds=[("/data", overlay_dir)],
    )

    assert handle.proc.pid == 7171
    assert captured["kwargs"]["stdout"] is None  # inherited, never PIPE-buffered
    assert overlay_dir in captured["argv"] and "/data" in captured["argv"]
    assert os.path.isdir(os.path.dirname(bus_socket))
    assert "vibecanvas_api.services.agent_runtime.sandbox_entry" in captured["argv"]
    assert "--share-net" in captured["argv"]  # host-network mode


def test_launch_agent_runtime_bus_rejects_snapshot(tmp_path):
    provider = _provider()
    snapshot = ServeSnapshot(image_dir=str(tmp_path), fingerprint="x")

    with pytest.raises(RuntimeError, match="rootful gVisor"):
        provider.launch_agent_runtime_bus(
            run_id="r",
            bus_socket=str(tmp_path / "bus.sock"),
            tenant="tenant-1",
            extra_rw_binds=[("/data", str(tmp_path / "data"))],
            snapshot=snapshot,
        )


def test_launch_workflow_bus_writes_job_files_and_binds_run_dir(monkeypatch, tmp_path):
    run_dir = str(tmp_path / "rundir")
    os.makedirs(run_dir)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return _FakeServeProcess(pid=8181)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    provider = _provider()

    # An empty workflow (no nodes) is trivially "pure" per classify_workflow
    # (workflow_guard.py) — this test exercises the bind/argv wiring, not
    # node classification, which is already covered elsewhere.
    handle = provider.launch_workflow_bus(
        run_dir=run_dir,
        workflow={},
        inputs={},
        run_id="wf-run-1",
        bus_socket=str(tmp_path / "bus" / "run.sock"),
    )

    assert handle.proc.pid == 8181
    assert os.path.isfile(os.path.join(run_dir, "__exec__", "workflow.json"))
    assert os.path.isfile(os.path.join(run_dir, "__exec__", "inputs.json"))
    assert run_dir in captured["argv"] and "/run" in captured["argv"]


def test_stop_run_kills_process_group_after_grace_timeout():
    provider = _provider()

    class _NeverExits(_FakeServeProcess):
        def wait(self, timeout=None):
            if timeout and timeout > 2.5:
                self.waited = True
                return 0
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    handle = BusRunHandle(
        proc=_NeverExits(pid=9191), bundle_dir="", state_root="",
        container_id="c", exec_dir="/tmp",
    )
    provider.stop_run(handle)  # must not raise even without os.killpg mocked


class _FakeServeProcess:
    def __init__(self, pid=4343):
        self.pid = pid
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True
        return 0


def test_run_serve_returns_handle_without_waiting(monkeypatch, tmp_path):
    runs_root = str(tmp_path / "runs")
    work_dir = str(tmp_path / "work")
    os.makedirs(runs_root)
    os.makedirs(work_dir)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeServeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    provider = _provider()

    skills_dir = str(tmp_path / "skills")
    os.makedirs(skills_dir)
    handle = provider.run_serve(runs_root=runs_root, work_dir=work_dir,
                                extra_ro_dest_binds=[("/skills", skills_dir)])

    mount = captured["argv"].index(skills_dir)
    assert captured["argv"][mount - 1:mount + 2] == ["--ro-bind", skills_dir, "/skills"]
    assert handle.proc.pid == 4343
    assert handle.run_id  # non-empty identifier, even if unused for teardown
    assert captured["kwargs"]["start_new_session"] is True
    assert "/runs" in captured["argv"] and runs_root in captured["argv"]
    assert "/work" in captured["argv"] and work_dir in captured["argv"]
    # default command boots the engine's serve loop
    assert "vibecanvas_engine.sandbox_entry" in captured["argv"]
    assert "serve" in captured["argv"]


def test_stop_serve_kills_process_group(monkeypatch):
    provider = _provider()
    handle = ServeHandle(
        proc=_FakeServeProcess(pid=5151),
        bundle_dir="",
        state_root="",
        run_id="abc",
        network=None,
    )
    killed = {}
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: killed.update(pgid=pgid, sig=sig)
    )

    provider.stop_serve(handle)

    assert killed == {"pgid": 5151, "sig": signal.SIGKILL}
    assert handle.proc.waited is True


def test_checkpoint_serve_is_a_documented_noop(tmp_path):
    provider = _provider()
    handle = ServeHandle(
        proc=_FakeServeProcess(), bundle_dir="", state_root="", run_id="x", network=None,
    )

    # Must not raise, and must not require the image_dir to exist/be empty
    # the way RootlessGvisorProvider's rootful checkpoint does.
    assert provider.checkpoint_serve(handle, image_dir=str(tmp_path / "missing")) is None


def test_restore_serve_reboots_via_run_serve(monkeypatch, tmp_path):
    runs_root = str(tmp_path / "runs")
    work_dir = str(tmp_path / "work")
    os.makedirs(runs_root)
    os.makedirs(work_dir)
    provider = _provider()
    sentinel = ServeHandle(
        proc=_FakeServeProcess(), bundle_dir="", state_root="", run_id="new", network=None,
    )
    calls = {}

    def fake_run_serve(**kwargs):
        calls.update(kwargs)
        return sentinel

    monkeypatch.setattr(provider, "run_serve", fake_run_serve)
    snapshot = ServeSnapshot(
        image_dir=str(tmp_path / "snapshot-that-never-existed"),
        fingerprint="unused",
    )

    result = provider.restore_serve(
        snapshot=snapshot, runs_root=runs_root, work_dir=work_dir,
        extra_ro_dest_binds=[("/skills", str(tmp_path / "skills"))],
    )

    assert result is sentinel
    assert calls["runs_root"] == runs_root
    assert calls["work_dir"] == work_dir
    assert calls["extra_ro_dest_binds"] == [("/skills", str(tmp_path / "skills"))]


def test_get_sandbox_provider_resolves_bubblewrap_when_configured(monkeypatch):
    from vibecanvas_api.config import config
    from vibecanvas_api.services.sandbox import get_sandbox_provider

    monkeypatch.setattr(config, "sandbox_runtime", "bubblewrap")
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox._resolve_bwrap",
        lambda: "/usr/bin/bwrap",
    )

    provider = get_sandbox_provider()

    assert isinstance(provider, BubblewrapProvider)
