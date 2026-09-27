import os
import asyncio
import json
import signal
import threading

import pytest

from vibecanvas_api.services.sandbox.gvisor import (
    RootlessGvisorProvider,
    SandboxResult,
    build_oci_config,
)
from vibecanvas_api.services.sandbox import get_sandbox_provider, SandboxUnavailable


def test_missing_warm_result_has_engine_error_without_stderr(tmp_path):
    result = RootlessGvisorProvider._read_engine_result(str(tmp_path), None)

    assert result.final_outputs == {}
    assert result.error_dict == {
        "__engine__": "sandbox job completed without result.json",
    }


def test_oci_config_has_run_bind_and_uid_map(tmp_path):
    run_dir = str(tmp_path / "rundir")
    os.makedirs(run_dir)
    cfg = build_oci_config(
        command=["sh", "-c", "echo hi"], env={"A": "1"}, run_dir=run_dir
    )
    mounts = {m["destination"]: m for m in cfg["mounts"]}
    assert mounts["/run"]["type"] == "bind" and mounts["/run"]["source"] == run_dir
    assert "rw" in mounts["/run"]["options"]
    assert cfg["process"]["args"] == ["sh", "-c", "echo hi"]
    assert "A=1" in cfg["process"]["env"]
    assert cfg["process"]["cwd"] == "/run"
    um = cfg["linux"]["uidMappings"][0]
    assert um["containerID"] == 0 and um["hostID"] == os.getuid() and um["size"] == 1
    assert cfg["root"]["readonly"] is True
    # host system dirs are read-only bound; host /run is NOT a source (B2)
    assert all(m["source"] != "/run" for m in cfg["mounts"])
    assert "PYTHONUNBUFFERED=1" in cfg["process"]["env"]


def test_oci_config_mounts_external_resolver_symlink_target(monkeypatch, tmp_path):
    from vibecanvas_api.services.sandbox import gvisor

    resolver = tmp_path / "wsl" / "resolv.conf"
    resolver.parent.mkdir()
    resolver.write_text("nameserver 192.0.2.53\n", encoding="utf-8")
    monkeypatch.setattr(gvisor, "_RESOLVER_CONFIG", str(resolver))

    cfg = build_oci_config(command=["true"], env=None, run_dir=str(tmp_path))
    mounts = {mount["destination"]: mount for mount in cfg["mounts"]}

    assert mounts[str(resolver)] == {
        "destination": str(resolver),
        "type": "bind",
        "source": str(resolver),
        "options": ["bind", "ro"],
    }


def test_rootful_oci_config_omits_rootless_uid_mappings(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cfg = build_oci_config(
        command=["true"], env=None, run_dir=str(run_dir), rootless=False
    )

    assert "uidMappings" not in cfg["linux"]
    assert "gidMappings" not in cfg["linux"]


def test_resolver_raises_when_runsc_absent(monkeypatch):
    monkeypatch.setattr("vibecanvas_api.services.sandbox._resolve_runsc", lambda: None)
    import pytest

    with pytest.raises(SandboxUnavailable):
        get_sandbox_provider()


def test_oci_config_two_root_run_and_mount_binds(tmp_path):
    run_dir = str(tmp_path / "rundir")
    os.makedirs(run_dir)
    data_dir = str(tmp_path / "datadir")
    os.makedirs(data_dir)
    cfg = build_oci_config(
        command=["sh", "-c", "echo hi"],
        env={"A": "1"},
        rw_binds=[("/run", run_dir), ("/mount", data_dir)],
    )
    mounts = {m["destination"]: m for m in cfg["mounts"]}
    assert mounts["/run"]["source"] == run_dir and "rw" in mounts["/run"]["options"]
    assert mounts["/mount"]["type"] == "bind" and mounts["/mount"]["source"] == data_dir
    assert "rw" in mounts["/mount"]["options"]
    # /run FIRST → cwd stays /run even with the second root bound.
    assert cfg["process"]["cwd"] == "/run"


def test_oci_config_tmp_channel_replaces_default_tmpfs(tmp_path):
    """Pure Chat code jobs may bind their narrow job channel at /tmp. That mount
    must replace the default tmpfs rather than producing duplicate /tmp mounts."""
    channel_dir = str(tmp_path / "channel")
    os.makedirs(channel_dir)
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    cfg = build_oci_config(
        command=["true"],
        env=None,
        rw_binds=[("/tmp", channel_dir), ("/data", data_dir)],
    )
    tmp_mounts = [m for m in cfg["mounts"] if m["destination"] == "/tmp"]
    assert tmp_mounts == [
        {
            "destination": "/tmp",
            "type": "bind",
            "source": channel_dir,
            "options": ["rbind", "rw"],
        }
    ]
    assert cfg["process"]["cwd"] == "/tmp"


def test_oci_config_deduplicates_nested_identity_readonly_binds(tmp_path):
    nested = tmp_path / "runtime" / "packages"
    nested.mkdir(parents=True)
    cfg = build_oci_config(
        command=["true"],
        env=None,
        run_dir=str(tmp_path),
        extra_ro_binds=[str(tmp_path / "runtime"), str(nested), str(nested)],
    )
    sources = [mount.get("source") for mount in cfg["mounts"]]

    assert sources.count(str(tmp_path / "runtime")) == 1
    assert str(nested) not in sources


def test_oci_mount_order_preserves_readonly_runtime_below_writable_tmp(tmp_path):
    runtime = tmp_path / "runtime"
    channel = tmp_path / "channel"
    runtime.mkdir()
    channel.mkdir()
    cfg = build_oci_config(
        command=["true"], env=None,
        rw_binds=[("/tmp", str(channel))],
        ro_binds=[("/tmp/runtime", str(runtime))],
    )
    relevant = [m for m in cfg["mounts"] if m["destination"].startswith("/tmp")]
    assert relevant == [
        {"destination": "/tmp", "source": str(channel), "type": "bind", "options": ["rbind", "rw"]},
        {"destination": "/tmp/runtime", "source": str(runtime), "type": "bind", "options": ["rbind", "ro"]},
    ]
    assert cfg["process"]["cwd"] == "/tmp"


def test_oci_mount_order_preserves_nested_writable_roots_and_cwd(tmp_path):
    outer = tmp_path / "outer"
    inner = tmp_path / "inner"
    outer.mkdir()
    inner.mkdir()
    cfg = build_oci_config(
        command=["true"], env=None,
        rw_binds=[("/data/nested", str(inner)), ("/data", str(outer))],
    )
    relevant = [m for m in cfg["mounts"] if m["destination"].startswith("/data")]
    assert relevant == [
        {"destination": "/data", "source": str(outer), "type": "bind", "options": ["rbind", "rw"]},
        {"destination": "/data/nested", "source": str(inner), "type": "bind", "options": ["rbind", "rw"]},
    ]
    # Choosing cwd from the first declared workspace is independent of ordering.
    assert cfg["process"]["cwd"] == "/data/nested"


def test_oci_config_rejects_duplicate_destinations(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with pytest.raises(ValueError, match="duplicate OCI mount destination"):
        build_oci_config(
            command=["true"],
            env=None,
            rw_binds=[("/data", str(first)), ("/data", str(second))],
        )


def test_oci_config_enforces_mount_limit(monkeypatch, tmp_path):
    from vibecanvas_api.services.sandbox import gvisor

    monkeypatch.setattr(gvisor.config, "sandbox_max_mounts", 8)
    roots = []
    for index in range(8):
        root = tmp_path / f"root-{index}"
        root.mkdir()
        roots.append(str(root))

    with pytest.raises(RuntimeError, match="sandbox mount limit exceeded"):
        build_oci_config(
            command=["true"], env=None, run_dir=str(tmp_path), extra_ro_binds=roots
        )


def test_run_accepts_explicit_mount_bind(monkeypatch, tmp_path):
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider

    run_dir = str(tmp_path / "rundir")
    os.makedirs(run_dir)
    mount_dir = str(tmp_path / "mount")
    os.makedirs(mount_dir)
    captured = {}

    prov = RootlessGvisorProvider(runsc_path="/nonexistent/runsc")

    def _fake_build_bundle(*, command, env, rw_binds, ro_binds=(), ro_dest_binds=None):
        captured["rw_binds"] = rw_binds
        # short-circuit the actual runsc spawn by raising after capture
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(prov, "_build_bundle", _fake_build_bundle)
    import pytest

    with pytest.raises(RuntimeError, match="stop-after-capture"):
        prov.run(
            run_dir=run_dir,
            command=["true"],
            extra_rw_binds=[("/mount", mount_dir)],
        )

    assert captured["rw_binds"] == [("/run", run_dir), ("/mount", mount_dir)]


def test_run_without_extra_binds_uses_only_run(monkeypatch, tmp_path):
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider

    run_dir = str(tmp_path / "rundir")
    os.makedirs(run_dir)
    captured = {}
    prov = RootlessGvisorProvider(runsc_path="/nonexistent/runsc")

    def _fake_build_bundle(*, command, env, rw_binds, ro_binds=(), ro_dest_binds=None):
        captured["rw_binds"] = rw_binds
        raise RuntimeError("stop")

    monkeypatch.setattr(prov, "_build_bundle", _fake_build_bundle)
    import pytest

    with pytest.raises(RuntimeError, match="stop"):
        prov.run(run_dir=run_dir, command=["true"])
    assert captured["rw_binds"] == [("/run", run_dir)]


def test_explicit_dependency_paths_still_mount_editable_sources(monkeypatch, tmp_path):
    """A configured site-packages path does not make editable source visible.

    Its ``.pth`` target lives outside the interpreter prefix, so gVisor needs
    both the source bind and the matching ``PYTHONPATH`` entry.
    """
    from vibecanvas_api.config import config
    from vibecanvas_api.services.sandbox import gvisor

    dependency_root = tmp_path / "environment" / "site-packages"
    engine_root = tmp_path / "repo" / "engine" / "src"
    api_root = tmp_path / "repo" / "api" / "src"
    for path in (dependency_root, engine_root, api_root):
        path.mkdir(parents=True)

    monkeypatch.setattr(config, "sandbox_python_paths", [str(dependency_root)])
    monkeypatch.setattr(
        gvisor,
        "_workflow_python_dependency_paths",
        lambda: [str(dependency_root)],
    )
    monkeypatch.setattr(
        gvisor,
        "_module_source_root",
        lambda name: {
            "vibecanvas_engine": str(engine_root),
            "vibecanvas_api": str(api_root),
        }[name],
    )
    monkeypatch.setattr(
        gvisor,
        "_runtime_has_installed_module",
        lambda _module_name, _roots: False,
    )

    assert gvisor._workflow_python_paths() == [
        str(engine_root),
        str(api_root),
    ]


def test_local_application_cache_skips_editable_source_binds(monkeypatch):
    from vibecanvas_api.services.sandbox import gvisor

    monkeypatch.setenv("VIBECANVAS_SANDBOX_USE_INSTALLED_APP", "1")
    monkeypatch.setattr(
        gvisor,
        "_module_source_root",
        lambda _name: "/shared/workspace/api/src",
    )

    assert gvisor._workflow_python_paths() == []


def test_runtime_install_detection_does_not_depend_on_host_import_order(tmp_path):
    from vibecanvas_api.services.sandbox import gvisor

    package = (
        tmp_path
        / "lib"
        / f"python{gvisor.sys.version_info.major}.{gvisor.sys.version_info.minor}"
        / "site-packages"
        / "vibecanvas_api"
    )
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    assert gvisor._runtime_has_installed_module(
        "vibecanvas_api",
        [str(tmp_path)],
    )


def _probe_request() -> dict:
    return {
        "prefix": "probe",
        "connection": {"transport": "stdio", "command": "true"},
        "timeout_s": 1.0,
    }


@pytest.mark.parametrize(
    ("exit_code", "result_payload", "expected_status"),
    [
        (0, {"status": "ok", "tool_count": 0, "tool_names": []}, "ok"),
        (7, None, "error: sandbox probe failed"),
        (-signal.SIGKILL, None, "error: handshake timed out"),
    ],
)
def test_mcp_probe_cleans_private_directory_on_every_terminal_result(
    monkeypatch,
    tmp_path,
    exit_code,
    result_payload,
    expected_status,
):
    from vibecanvas_api.services.sandbox import gvisor

    monkeypatch.setattr(gvisor.tempfile, "tempdir", str(tmp_path))
    provider = RootlessGvisorProvider(runsc_path="/nonexistent/runsc")
    observed: list[str] = []

    def fake_run(*, run_dir, **_kwargs):
        observed.append(run_dir)
        request_path = os.path.join(run_dir, "request.json")
        assert oct(os.stat(request_path).st_mode & 0o777) == "0o600"
        if result_payload is not None:
            with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as handle:
                json.dump(result_payload, handle)
        return SandboxResult(exit_code, "", "probe failure", 0.01)

    monkeypatch.setattr(provider, "run", fake_run)
    result = provider.run_mcp_probe(
        request=_probe_request(),
        timeout=1.0,
        allow_hosts=set(),
    )

    assert result["status"].startswith(expected_status)
    assert len(observed) == 1
    assert not os.path.exists(observed[0])


@pytest.mark.asyncio
async def test_cancelled_mcp_probe_still_finishes_one_shot_cleanup(
    monkeypatch,
    tmp_path,
):
    from vibecanvas_api.services.sandbox import gvisor

    monkeypatch.setattr(gvisor.tempfile, "tempdir", str(tmp_path))
    provider = RootlessGvisorProvider(runsc_path="/nonexistent/runsc")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observed: list[str] = []

    def fake_run(*, run_dir, **_kwargs):
        observed.append(run_dir)
        started.set()
        release.wait(timeout=5)
        return SandboxResult(0, "", "", 0.01)

    monkeypatch.setattr(provider, "run", fake_run)

    def invoke():
        try:
            provider.run_mcp_probe(
                request=_probe_request(),
                timeout=1.0,
                allow_hosts=set(),
            )
        finally:
            finished.set()

    task = asyncio.create_task(asyncio.to_thread(invoke))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    assert len(observed) == 1
    assert not os.path.exists(observed[0])
