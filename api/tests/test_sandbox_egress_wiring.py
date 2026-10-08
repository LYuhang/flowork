# -*- coding: utf-8 -*-
"""Plan-B B5 — gVisor provider proxy-mode wiring (unit, NO gVisor).

These tests stub :meth:`RootlessGvisorProvider.run` to CAPTURE the argv/env/binds
without launching runsc, and spy the :class:`EgressBroker` so we can assert its
start/aclose lifecycle. Three scenarios:

  * proxy mode + allow_hosts → ``--network=none``, proxy env, ``/vcegress`` rw
    bind, ``--host-uds=open``, broker started + aclosed.
  * host-network mode (DEFAULT) + allow_hosts → byte-identical to today (NO
    network override, NO proxy env, NO egress bind, NO broker). Proves dev path
    untouched.
  * proxy mode but allow_hosts=None → defensive: no egress setup at all.
"""

from __future__ import annotations

import json
import os

import pytest

from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox import gvisor as gvisor_mod
from vibecanvas_api.services.sandbox.gvisor import (
    IN_SANDBOX_EGRESS_DIR,
    RootlessGvisorProvider,
    _EGRESS_PROXY_PORT,
)
from vibecanvas_api.services.sandbox.provider import SandboxResult


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _trivial_pure_wf() -> dict:
    """Start -> Code(returns {'v': x+1}) -> End. No file I/O."""
    return {
        "__meta__": {
            "workflow_id": "wf_egress",
            "workflow_name": "egress_wiring",
            "workflow_version": 1,
            "workflow_subversion": 0,
        },
        "node_1": {
            "node_id": "node_1", "node_name": "__start__", "node_type": "StartNode",
            "node_description": "start", "input_fields": {},
            "output_fields": {"x": {"type": "integer", "description": "n"}},
            "node_config": {}, "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2", "node_name": "compute", "node_type": "CodeNode",
            "node_description": "increment x",
            "input_fields": {"x": {"type": "integer", "value": 0, "reference": "__start__.x"}},
            "output_fields": {"v": {"type": "integer", "description": "x + 1"}},
            "node_config": {
                "programming_language": "python",
                "process_fn": "def process_fn(inputs):\n    return {'v': inputs['x'] + 1}",
            },
            "children": ["node_3"],
        },
        "node_3": {
            "node_id": "node_3", "node_name": "__end__", "node_type": "EndNode",
            "node_description": "end",
            "input_fields": {"v": {"type": "integer", "value": 0, "reference": "compute.v"}},
            "output_fields": {"v": {"type": "integer", "description": "x + 1"}},
            "node_config": {}, "children": [],
        },
    }


class _SpyBroker:
    """Records every EgressBroker(...) construction + start()/aclose() call.

    Instances append to the class-level ``instances`` list so a test can inspect
    construction args + lifecycle ordering after the run."""

    instances: list = []

    def __init__(
        self,
        socket_path,
        *,
        allow_hosts,
        run_id,
        allow_private_targets=None,
        allow_public=False,
        **_kwargs,
    ):
        self.socket_path = socket_path
        self.allow_hosts = set(allow_hosts)
        self.run_id = run_id
        self.allow_private_targets = set(allow_private_targets or ())
        self.allow_public = bool(allow_public)
        self.started = False
        self.closed = False
        type(self).instances.append(self)

    async def start(self):
        self.started = True

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_spy():
    _SpyBroker.instances = []
    yield
    _SpyBroker.instances = []


def _stub_run(captured: dict):
    """A fake ``run`` that records argv-relevant kwargs + writes a clean
    result.json so ``_read_engine_result`` parses (mirrors the overlay-network
    test's fake_run; absorbs ALL kwargs with **kw)."""

    def fake_run(*, run_dir, command, env=None, network=None, timeout=60.0,
                 extra_ro_binds=(), data_dir=None, extra_rw_binds=None,
                 bus_socket=None, egress_socket=None, **kw):
        captured["command"] = command
        captured["env"] = dict(env or {})
        captured["network"] = network
        captured["extra_rw_binds"] = list(extra_rw_binds or [])
        captured["bus_socket"] = bus_socket
        captured["egress_socket"] = egress_socket
        exec_dir = os.path.join(run_dir, "__exec__")
        os.makedirs(exec_dir, exist_ok=True)
        with open(os.path.join(exec_dir, "result.json"), "w") as f:
            json.dump({"final_outputs": {}, "error_dict": {}, "execution_time": 0.0}, f)
        with open(os.path.join(exec_dir, "events.ndjson"), "w") as f:
            f.write("")
        return SandboxResult(exit_code=0, stdout="", stderr="", duration_s=0.0)

    return fake_run


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_stop_run_tears_down_egress_loop_thread_idempotent(tmp_path, monkeypatch):
    """MINOR-1 — bus-path egress broker teardown on stream abandon.

    The bus-path backend (``OneShotBusBackend``) calls ``provider.stop_run`` in
    its ``finally`` — which runs even when the stream generator is ABANDONED
    (GeneratorExit / early close) or cancelled. So the narrow load-bearing
    invariant is: ``stop_run`` tears down the per-run egress broker loop thread
    carried on the handle, and is idempotent (safe to call twice).

    This is the focused unit on the handle teardown (per the review's "narrower
    invariant" allowance) — no full proxy-mode bus run needed: a real bus run
    requires gVisor. We build a ``BusRunHandle`` with a spy loop thread and
    assert stop_run stops it. ``os.killpg`` / ``subprocess.run`` / ``rmtree`` are
    stubbed so no real process / fs is touched."""
    from vibecanvas_api.services.sandbox.gvisor import BusRunHandle

    class _SpyLoopThread:
        def __init__(self):
            self.stops = 0

        def stop(self):
            self.stops += 1

    spy = _SpyLoopThread()

    class _FakeProc:
        pid = 999999  # a pid os.getpgid will be stubbed away from touching.

    handle = BusRunHandle(
        proc=_FakeProc(),
        bundle_dir=str(tmp_path / "bundle"),
        state_root=str(tmp_path / "root"),
        container_id="cid-egress",
        exec_dir=str(tmp_path / "__exec__"),
        egress_loop_thread=spy,
    )

    # Neutralize the real process-group kill + runsc delete + rmtree.
    monkeypatch.setattr(gvisor_mod.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(gvisor_mod.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(gvisor_mod.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(gvisor_mod.shutil, "rmtree", lambda *a, **k: None)

    prov = RootlessGvisorProvider("/nonexistent/runsc")

    # Abandon/cancel path: stop_run with kill=True (what the backend's finally
    # passes when the run did NOT reach a clean result).
    prov.stop_run(handle, kill=True)
    assert spy.stops == 1, "egress loop thread must be torn down on stop_run"

    # Idempotent — a second teardown (e.g. a defensive double-close) is safe and
    # still stops the (now already-stopped, best-effort) loop thread.
    prov.stop_run(handle, kill=False)
    assert spy.stops == 2


def test_proxy_mode_sets_network_env_bind(tmp_path, monkeypatch):
    """proxy mode + allow_hosts → network=none, proxy env, /vcegress rw bind,
    EgressBroker started + aclosed."""
    monkeypatch.setattr(config, "sandbox_egress_mode", "proxy")
    monkeypatch.setattr(config, "sandbox_egress_policy", "allowlist")
    monkeypatch.setattr(config, "sandbox_egress_allow_hosts", ())
    monkeypatch.setattr(gvisor_mod, "EgressBroker", _SpyBroker)

    prov = RootlessGvisorProvider("/nonexistent/runsc")
    captured: dict = {}
    monkeypatch.setattr(prov, "run", _stub_run(captured))

    prov.run_workflow(
        run_dir=str(tmp_path), workflow=_trivial_pure_wf(), inputs={"x": 1},
        run_id="run-egress-1", allow_hosts={"llm.test"},
    )

    # 1. network forced to none
    assert captured["network"] == "none"
    # 2. proxy env injected
    env = captured["env"]
    proxy_url = f"http://127.0.0.1:{_EGRESS_PROXY_PORT}"
    assert env["HTTP_PROXY"] == proxy_url
    assert env["HTTPS_PROXY"] == proxy_url
    assert env["NO_PROXY"] == "127.0.0.1,localhost"
    assert env["VC_EGRESS_SOCK"] == IN_SANDBOX_EGRESS_DIR + "/egress.sock"
    assert env["VC_EGRESS_PORT"] == str(_EGRESS_PROXY_PORT)
    # 3. the egress socket is passed to run() (run() binds its dir at /vcegress
    #    AND appends --host-uds=open — verified end-to-end in the flag test).
    assert captured["egress_socket"] is not None
    # 4. broker lifecycle: exactly one started + aclosed, with our allowlist.
    assert len(_SpyBroker.instances) == 1
    b = _SpyBroker.instances[0]
    assert b.started and b.closed
    assert b.allow_hosts == {"llm.test"}
    assert b.allow_public is False
    platform_origin = gvisor_mod.urlsplit(config.mcp.platform_internal_base_url)
    browser_origin = gvisor_mod.urlsplit(config.browser_gateway_internal_base_url)
    assert b.allow_private_targets == {
        (browser_origin.hostname.lower(), browser_origin.port or (443 if browser_origin.scheme == "https" else 80)),
        (
            platform_origin.hostname.lower(),
            platform_origin.port
            or (443 if platform_origin.scheme == "https" else 80),
        )
    }
    assert b.run_id == "run-egress-1"
    # the broker bound the SAME socket path that run() was told to bind.
    assert b.socket_path == captured["egress_socket"]


def test_public_policy_is_workload_and_lifecycle_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "sandbox_egress_mode", "proxy")
    monkeypatch.setattr(config, "sandbox_egress_policy", "public")
    monkeypatch.setattr(config, "sandbox_egress_allow_hosts", ())
    monkeypatch.setattr(config, "sandbox_egress_private_targets", ())
    monkeypatch.setattr(gvisor_mod, "EgressBroker", _SpyBroker)
    provider = RootlessGvisorProvider("/nonexistent/runsc")

    setups = [
        provider._sandbox_egress_setup("oneshot", {"workflow.example"}),
        provider._sandbox_egress_setup("resident", set()),
        provider._sandbox_egress_setup("agent", {"tool.example"}),
    ]
    try:
        assert len(_SpyBroker.instances) == 3
        assert all(b.allow_public for b in _SpyBroker.instances)
        assert all(b.allow_hosts == set() for b in _SpyBroker.instances)
        assert all(setup is not None for setup in setups)
    finally:
        for setup in setups:
            if setup is not None:
                setup[0].stop()


def test_proxy_mode_host_uds_flag_set(tmp_path, monkeypatch):
    """--host-uds=open MUST be set in proxy mode. We assert by inspecting the
    REAL run() argv assembly (bus_socket path appends the flag): pass the
    captured bus_socket through a fresh provider whose _build_bundle + Popen are
    stubbed, and check the argv. Simpler: assert run() receives a bus_socket
    (proven above) — run() already appends --host-uds=open whenever bus_socket is
    set (gvisor.py). Here we directly verify run()'s flag logic end to end."""
    monkeypatch.setattr(config, "sandbox_egress_mode", "proxy")
    monkeypatch.setattr(gvisor_mod, "EgressBroker", _SpyBroker)

    prov = RootlessGvisorProvider("/nonexistent/runsc")

    # Capture the runsc argv by stubbing _build_bundle + subprocess.Popen.
    seen = {}

    real_build = prov._build_bundle

    def spy_build(**kw):
        bundle, state_root, run_id = real_build(**kw)
        seen["rw_binds"] = kw["rw_binds"]
        return bundle, state_root, run_id

    monkeypatch.setattr(prov, "_build_bundle", spy_build)

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return ("", "")

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        # write a result so _read_engine_result parses
        return _FakeProc()

    monkeypatch.setattr(gvisor_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(gvisor_mod.subprocess, "run", lambda *a, **k: None)

    # run_workflow writes __exec__ files; the FakeProc never writes result.json
    # so we get an engine-error result — that's fine, we only inspect argv.
    prov.run_workflow(
        run_dir=str(tmp_path), workflow=_trivial_pure_wf(), inputs={"x": 1},
        run_id="run-egress-flag", allow_hosts={"llm.test"},
    )

    assert "--host-uds=open" in seen["argv"], seen["argv"]
    assert "--network=none" in seen["argv"], seen["argv"]
    # the /vcegress dir was bound rw
    dests = {dest for dest, _src in seen["rw_binds"]}
    assert IN_SANDBOX_EGRESS_DIR in dests


def test_host_network_mode_unchanged(tmp_path, monkeypatch):
    """host-network mode (DEFAULT) + allow_hosts → NO network override (stays
    None→config default), NO proxy env, NO egress bind, NO broker. Proves the dev
    path is byte-identical to today even when allow_hosts is supplied."""
    monkeypatch.setattr(config, "sandbox_egress_mode", "host-network")
    monkeypatch.setattr(gvisor_mod, "EgressBroker", _SpyBroker)

    prov = RootlessGvisorProvider("/nonexistent/runsc")
    captured: dict = {}
    monkeypatch.setattr(prov, "run", _stub_run(captured))

    prov.run_workflow(
        run_dir=str(tmp_path), workflow=_trivial_pure_wf(), inputs={"x": 1},
        run_id="run-hostnet", allow_hosts={"llm.test"},
    )

    # network NOT overridden (None → run() resolves config.sandbox_network)
    assert captured["network"] is None
    env = captured["env"]
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "VC_EGRESS_SOCK", "VC_EGRESS_PORT"):
        assert k not in env, f"{k} must NOT be set in host-network mode"
    dests = {dest for dest, _src in captured["extra_rw_binds"]}
    assert IN_SANDBOX_EGRESS_DIR not in dests
    assert captured["egress_socket"] is None
    assert _SpyBroker.instances == []


def test_allow_hosts_none_still_uses_fail_closed_proxy(tmp_path, monkeypatch):
    """Proxy mode never silently degrades to a disconnected/direct worker."""
    monkeypatch.setattr(config, "sandbox_egress_mode", "proxy")
    monkeypatch.setattr(gvisor_mod, "EgressBroker", _SpyBroker)

    prov = RootlessGvisorProvider("/nonexistent/runsc")
    captured: dict = {}
    monkeypatch.setattr(prov, "run", _stub_run(captured))

    prov.run_workflow(
        run_dir=str(tmp_path), workflow=_trivial_pure_wf(), inputs={"x": 1},
        run_id="run-none", allow_hosts=None,
    )

    assert captured["network"] == "none"
    env = captured["env"]
    assert env["HTTP_PROXY"].startswith("http://127.0.0.1:")
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]
    assert captured["egress_socket"] is not None
    assert len(_SpyBroker.instances) == 1
    assert _SpyBroker.instances[0].allow_hosts == set()
