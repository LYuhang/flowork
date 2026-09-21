"""RE-6 P2 T2 — run a PURE-ENGINE workflow INSIDE a gVisor sandbox.

Two layers:

1. **Unit (no gVisor):** ``build_oci_config(..., extra_ro_binds=...)`` appends a
   read-only bind for each host path; ``run_workflow``'s pure-engine guard
   raises ``EngineNeedsHostNode`` for an api-defined node and passes (reaches
   ``self.run``) for an all-pure workflow.

2. **gVisor (skipif not ``_gvisor_runnable()``):**
   - **(a) import-inside smoke (B1 gate):** ``import vibecanvas_engine`` exits 0
     INSIDE the sandbox with the host ``sys.path``/``sys.prefix`` read-only
     binds + ``PYTHONPATH``. This is the make-or-break: P1 binds only
     ``/bin /usr ...`` so the editable engine + user-site deps are invisible.
   - **(b) workflow ran INSIDE:** a pure Start->Code->End wf whose CodeNode
     writes the gVisor kernel release into ``/run/out.txt`` →
     ``EngineRunResult.final_outputs`` correct AND ``out.txt`` contains
     ``gvisor`` (proves it executed inside the gVisor kernel, not the host).
   - **(c) crash:** a CodeNode that raises → ``error_dict`` non-empty, no host
     crash.
   - **(d) overhead:** N>=5 trivial pure wf; print min/median of
     ``run_workflow`` (sandbox) vs in-process ``Workflow(wf).trigger(inputs)``,
     plus the host-side bundle/marshalling slice; sanity bound only.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

import pytest

from vibecanvas_api.services.sandbox import (
    EngineNeedsHostNode,
    EngineRunResult,
    _gvisor_runnable,
    build_oci_config,
    get_sandbox_provider,
)
from vibecanvas_api.services.sandbox.gvisor import (
    _workflow_python_binds,
    _workflow_python_env,
)


# ---------------------------------------------------------------------------
# workflow builders (mirror the engine test_sandbox_entry shapes)
# ---------------------------------------------------------------------------
def _meta() -> dict:
    return {
        "workflow_id": "wf_run_workflow",
        "workflow_name": "run_workflow_smoke",
        "workflow_version": 1,
        "workflow_subversion": 0,
    }


def _start(children, *, out=None) -> dict:
    return {
        "node_id": "node_1",
        "node_name": "__start__",
        "node_type": "StartNode",
        "node_description": "start",
        "input_fields": {},
        "output_fields": out or {},
        "node_config": {},
        "children": children,
    }


def _end(input_fields, output_fields) -> dict:
    return {
        "node_id": "node_3",
        "node_name": "__end__",
        "node_type": "EndNode",
        "node_description": "end",
        "input_fields": input_fields,
        "output_fields": output_fields,
        "node_config": {},
        "children": [],
    }


def _trivial_pure_wf() -> dict:
    """Start -> Code(returns {'v': x+1}) -> End. No file I/O."""
    return {
        "__meta__": _meta(),
        "node_1": _start(["node_2"], out={"x": {"type": "integer", "description": "n"}}),
        "node_2": {
            "node_id": "node_2",
            "node_name": "compute",
            "node_type": "CodeNode",
            "node_description": "increment x",
            "input_fields": {
                "x": {"type": "integer", "value": 0, "reference": "__start__.x"},
            },
            "output_fields": {"v": {"type": "integer", "description": "x + 1"}},
            "node_config": {
                "programming_language": "python",
                "process_fn": "def process_fn(inputs):\n    return {'v': inputs['x'] + 1}",
            },
            "children": ["node_3"],
        },
        "node_3": _end(
            {"v": {"type": "integer", "value": 0, "reference": "compute.v"}},
            {"v": {"type": "integer", "description": "x + 1"}},
        ),
    }


def _writes_kernel_wf() -> dict:
    """Start -> Code -> End where the CodeNode writes the kernel release string
    (e.g. ``4.19.0-gvisor`` INSIDE gVisor, via ``os.uname().release``) into
    ``/run/out.txt``. CodeNode runs as normal Python in a worker
    subprocess (no jail) — so it ``import os`` itself and ``open('/run/out.txt')``
    is a REAL path; inside the sandbox the run-tier is bind-mounted at ``/run`` so
    the file lands on the host run-tier and is visible host-side.
    Returns {'rel': <release>}."""
    code = (
        "import os\n"
        "def process_fn(inputs):\n"
        "    rel = os.uname().release\n"
        "    open('/run/out.txt', 'w').write(rel)\n"
        "    return {'rel': rel}\n"
    )
    return {
        "__meta__": _meta(),
        "node_1": _start(["node_2"]),
        "node_2": {
            "node_id": "node_2",
            "node_name": "kernel",
            "node_type": "CodeNode",
            "node_description": "write kernel release to /run/out.txt",
            "input_fields": {},
            "output_fields": {"rel": {"type": "string", "description": "kernel release"}},
            "node_config": {
                "programming_language": "python",
                "process_fn": code,
            },
            "children": ["node_3"],
        },
        "node_3": _end(
            {"rel": {"type": "string", "value": "", "reference": "kernel.rel"}},
            {"rel": {"type": "string", "description": "kernel release"}},
        ),
    }


def _crashing_wf() -> dict:
    """Start -> Code(raises) -> End."""
    return {
        "__meta__": _meta(),
        "node_1": _start(["node_2"]),
        "node_2": {
            "node_id": "node_2",
            "node_name": "boom",
            "node_type": "CodeNode",
            "node_description": "raises",
            "input_fields": {},
            "output_fields": {"v": {"type": "integer", "description": "never"}},
            "node_config": {
                "programming_language": "python",
                "process_fn": "def process_fn(inputs):\n    raise RuntimeError('kaboom')",
            },
            "children": ["node_3"],
        },
        "node_3": _end(
            {"v": {"type": "integer", "value": 0, "reference": "boom.v"}},
            {"v": {"type": "integer", "description": "never"}},
        ),
    }


def _mount_io_wf() -> dict:
    """Start -> Code(read/write /mount) -> End."""
    code = (
        "def process_fn(inputs):\n"
        "    with open('/mount/input.txt', 'r', encoding='utf-8') as f:\n"
        "        value = f.read()\n"
        "    with open('/mount/workflow-output.txt', 'w', encoding='utf-8') as f:\n"
        "        f.write(value + '-workflow')\n"
        "    return {'value': value}\n"
    )
    return {
        "__meta__": _meta(),
        "node_1": _start(["node_2"]),
        "node_2": {
            "node_id": "node_2",
            "node_name": "mount_io",
            "node_type": "CodeNode",
            "node_description": "read and write the user mount",
            "input_fields": {},
            "output_fields": {
                "value": {"type": "string", "description": "mounted value"}
            },
            "node_config": {
                "programming_language": "python",
                "process_fn": code,
            },
            "children": ["node_3"],
        },
        "node_3": _end(
            {
                "value": {
                    "type": "string",
                    "value": "",
                    "reference": "mount_io.value",
                }
            },
            {"value": {"type": "string", "description": "mounted value"}},
        ),
    }


def _wf_with_api_node() -> dict:
    """A workflow with a HOST-ONLY node_type — one that is neither pure-engine
    nor in the engine-native sandbox allowlist. Host/API data nodes require a
    broker and cannot receive a database-role exception."""
    wf = _trivial_pure_wf()
    wf["node_2"]["node_type"] = "HostOnlyMadeUpNode"
    return wf


# ===========================================================================
# Unit — no gVisor
# ===========================================================================
def test_build_oci_config_appends_extra_ro_binds(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = build_oci_config(
        command=["true"],
        env={"PYTHONPATH": "/x"},
        run_dir=str(tmp_path),
        extra_ro_binds=[str(a), str(b)],
    )
    binds = {
        m["source"]: m
        for m in cfg["mounts"]
        if m.get("type") == "bind"
    }
    for src in (str(a), str(b)):
        assert src in binds, f"{src} not bind-mounted"
        m = binds[src]
        assert m["destination"] == src, "dest must equal source (host path identity)"
        assert "ro" in m["options"], "extra binds must be read-only"
    # env still threads through
    assert any(e == "PYTHONPATH=/x" for e in cfg["process"]["env"])
    # nonexistent paths are filtered (consistent with P1 _HOST_RO_BINDS)
    cfg2 = build_oci_config(
        command=["true"], env=None, run_dir=str(tmp_path),
        extra_ro_binds=["/no/such/dir/xyz"],
    )
    assert "/no/such/dir/xyz" not in {m.get("source") for m in cfg2["mounts"]}


def test_build_oci_config_default_no_extra_binds(tmp_path):
    """Back-compat: omitting extra_ro_binds yields the P1 mount set (no extras)."""
    cfg = build_oci_config(command=["true"], env=None, run_dir=str(tmp_path))
    sources = {m.get("source") for m in cfg["mounts"]}
    assert "/usr" in sources  # P1 host bind still present
    assert "/tmp/x" not in sources


def test_run_workflow_guard_rejects_api_node(tmp_path):
    """The pure-engine guard raises EngineNeedsHostNode for an api-defined node,
    BEFORE ever touching the sandbox (no self.run call)."""
    # Build a provider directly (don't require runsc for the guard test).
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider

    prov = RootlessGvisorProvider("/nonexistent/runsc")
    called = {"run": False}
    prov.run = lambda **kw: called.__setitem__("run", True)  # type: ignore[method-assign]

    with pytest.raises(EngineNeedsHostNode):
        prov.run_workflow(
            run_dir=str(tmp_path),
            workflow=_wf_with_api_node(),
            inputs={},
            run_id="r1",
        )
    assert called["run"] is False, "guard must reject BEFORE launching the sandbox"


def test_run_workflow_guard_passes_pure(tmp_path, monkeypatch):
    """An all-pure workflow passes the guard and reaches self.run (monkeypatched
    to stop before the real sandbox)."""
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider
    from vibecanvas_api.services.sandbox.provider import SandboxResult

    prov = RootlessGvisorProvider("/nonexistent/runsc")
    captured = {}

    def fake_run(*, run_dir, command, env=None, network="host", timeout=60.0, extra_ro_binds=()):
        captured["command"] = command
        captured["env"] = env
        captured["extra_ro_binds"] = list(extra_ro_binds)
        # the host wrote workflow.json/inputs.json — simulate the entrypoint by
        # NOT writing result.json (exercise the missing-result path is a
        # separate test); here just write a clean empty result so it parses.
        exec_dir = os.path.join(run_dir, "__exec__")
        with open(os.path.join(exec_dir, "result.json"), "w") as f:
            json.dump({"final_outputs": {"__end__": {"v": 2}}, "error_dict": {}, "execution_time": 0.01}, f)
        with open(os.path.join(exec_dir, "events.ndjson"), "w") as f:
            f.write("")
        return SandboxResult(exit_code=0, stdout="", stderr="", duration_s=0.01)

    monkeypatch.setattr(prov, "run", fake_run)

    res = prov.run_workflow(
        run_dir=str(tmp_path),
        workflow=_trivial_pure_wf(),
        inputs={"x": 1},
        run_id="r1",
    )
    assert isinstance(res, EngineRunResult)
    assert res.final_outputs == {"__end__": {"v": 2}}
    assert res.error_dict == {}
    # the host materialized the exec channel + chose sys.executable + binds
    assert captured["command"][0] == sys.executable
    assert captured["command"][1:] == ["-m", "vibecanvas_engine.sandbox_entry", "r1"]
    assert "VC_SANDBOX_PYTHON_PATHS" in captured["env"]
    assert captured["extra_ro_binds"], "must bind the host python env"
    # Editable development installs need their application source roots in
    # PYTHONPATH. Dependency roots remain isolated in VC_SANDBOX_PYTHON_PATHS
    # so a third-party package cannot shadow the standard library at startup.
    source_paths = [
        value for value in captured["env"].get("PYTHONPATH", "").split(os.pathsep)
        if value
    ]
    dependency_paths = {
        value for value in captured["env"]["VC_SANDBOX_PYTHON_PATHS"].split(os.pathsep)
        if value
    }
    assert all(os.path.isabs(path) for path in source_paths)
    assert all("site-packages" not in Path(path).parts for path in source_paths)
    assert dependency_paths.isdisjoint(source_paths)
    assert all(path in captured["extra_ro_binds"] for path in source_paths)
    # host wrote workflow.json + inputs.json
    assert json.loads((tmp_path / "__exec__" / "inputs.json").read_text()) == {"x": 1}


def test_run_workflow_missing_result_is_engine_error(tmp_path, monkeypatch):
    """If the sandbox crashes pre-write (no result.json) → an engine-error
    EngineRunResult carrying the stderr, not an exception."""
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider
    from vibecanvas_api.services.sandbox.provider import SandboxResult

    prov = RootlessGvisorProvider("/nonexistent/runsc")

    def fake_run(*, run_dir, command, env=None, network="host", timeout=60.0, extra_ro_binds=()):
        return SandboxResult(exit_code=1, stdout="", stderr="boom-traceback", duration_s=0.01)

    monkeypatch.setattr(prov, "run", fake_run)
    res = prov.run_workflow(
        run_dir=str(tmp_path), workflow=_trivial_pure_wf(), inputs={}, run_id="r1"
    )
    assert res.final_outputs == {}
    assert "boom-traceback" in res.error_dict.get("__engine__", "")


def test_run_workflow_mounts_user_workspace(tmp_path, monkeypatch):
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider
    from vibecanvas_api.services.sandbox.provider import SandboxResult

    provider = RootlessGvisorProvider("/nonexistent/runsc")
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        exec_dir = os.path.join(kwargs["run_dir"], "__exec__")
        with open(os.path.join(exec_dir, "result.json"), "w") as file:
            json.dump({"final_outputs": {}, "error_dict": {}, "execution_time": 0}, file)
        with open(os.path.join(exec_dir, "events.ndjson"), "w") as file:
            file.write("")
        return SandboxResult(exit_code=0, stdout="", stderr="", duration_s=0)

    monkeypatch.setattr(provider, "run", fake_run)
    mount_dir = tmp_path / "mount"
    mount_dir.mkdir()
    provider.run_workflow(
        run_dir=str(tmp_path / "run"),
        workflow=_trivial_pure_wf(),
        inputs={},
        run_id="mounted",
        mount_dir=str(mount_dir),
    )

    assert captured["extra_rw_binds"] == [("/mount", str(mount_dir))]


# ===========================================================================
# gVisor — real runsc
# ===========================================================================
gvisor = pytest.mark.skipif(not _gvisor_runnable(), reason="rootless gVisor not runnable here")


@gvisor
def test_import_engine_inside_sandbox_smoke(tmp_path):
    """B1 GATE: import vibecanvas_engine (editable, /mnt) + user-site deps
    (requests, json_repair, /home) must succeed INSIDE the sandbox once the host
    sys.path/prefix dirs are bound read-only + PYTHONPATH is set. A bind miss is
    an explicit failure here, not an opaque crash in the workflow test."""
    r = get_sandbox_provider().run(
        run_dir=str(tmp_path),
        command=[
            sys.executable,
            "-c",
            "import vibecanvas_engine, requests, json_repair; print('OK', vibecanvas_engine.__file__)",
        ],
        env=_workflow_python_env(),
        extra_ro_binds=_workflow_python_binds(),
    )
    assert r.exit_code == 0, f"import FAILED inside sandbox:\nSTDERR:\n{r.stderr}"
    assert "OK" in r.stdout, f"missing OK marker; stdout={r.stdout!r}"


@gvisor
def test_run_workflow_executes_inside_gvisor(tmp_path):
    """B: a pure wf runs INSIDE gVisor — final_outputs correct AND the CodeNode's
    /run/out.txt (the gVisor kernel release) lands host-side containing 'gvisor'
    (proves the engine kernel was gVisor's, not the host's)."""
    res = get_sandbox_provider().run_workflow(
        run_dir=str(tmp_path),
        workflow=_writes_kernel_wf(),
        inputs={},
        run_id="rk",
        timeout=120.0,
    )
    assert res.error_dict == {}, f"engine errors: {res.error_dict}\nsandbox.stderr={res.sandbox.stderr}"
    rel = res.final_outputs.get("__end__", {}).get("rel", "")
    assert "gvisor" in rel.lower(), f"final_outputs kernel release not gVisor: {rel!r}"
    out = (tmp_path / "out.txt")
    assert out.exists(), "CodeNode did not write /run/out.txt host-side"
    assert "gvisor" in out.read_text().lower()
    assert isinstance(res.events, list) and res.events, "events.ndjson not read back"


@gvisor
def test_run_workflow_crash_is_contained(tmp_path):
    """C: a CodeNode that raises → non-empty error_dict, no host crash, a clean
    EngineRunResult."""
    res = get_sandbox_provider().run_workflow(
        run_dir=str(tmp_path),
        workflow=_crashing_wf(),
        inputs={},
        run_id="rc",
        timeout=120.0,
    )
    assert res.error_dict, "a raising CodeNode must surface in error_dict"
    assert isinstance(res, EngineRunResult)


@gvisor
def test_run_workflow_overhead(tmp_path):
    """D: N>=5 trivial pure wf — print min/median run_workflow (sandbox) vs
    in-process Workflow(wf).trigger(inputs), plus the host-side bundle/marshal
    slice. Sanity bound only (no perf regression assertion)."""
    from vibecanvas_engine.workflow import Workflow

    provider = get_sandbox_provider()
    wf = _trivial_pure_wf()
    inputs = {"x": 1}

    # warm one sandbox run (rootfs page cache, import warmup)
    provider.run_workflow(run_dir=str(tmp_path / "warm"), workflow=wf, inputs=inputs, run_id="warm", timeout=120.0)

    n = 6
    sbx_times: list[float] = []
    for i in range(n):
        d = tmp_path / f"s{i}"
        d.mkdir()
        t = time.monotonic()
        res = provider.run_workflow(run_dir=str(d), workflow=wf, inputs=inputs, run_id=f"s{i}", timeout=120.0)
        sbx_times.append(time.monotonic() - t)
        assert res.error_dict == {}, f"run {i} errored: {res.error_dict}"

    inproc_times: list[float] = []
    for _ in range(n):
        t = time.monotonic()
        Workflow(json.loads(json.dumps(wf))).trigger(inputs)
        inproc_times.append(time.monotonic() - t)

    print(
        "\n[RE-6 P2] run_workflow (gVisor, cold per-run) "
        f"min={min(sbx_times):.3f}s median={statistics.median(sbx_times):.3f}s\n"
        "[RE-6 P2] in-process Workflow.trigger              "
        f"min={min(inproc_times):.3f}s median={statistics.median(inproc_times):.3f}s\n"
        f"[RE-6 P2] sandbox/in-process median overhead factor "
        f"= {statistics.median(sbx_times) / max(statistics.median(inproc_times), 1e-6):.1f}x "
        "(cold per-run, NOT a warm-pool amortized cost)"
    )
    assert min(sbx_times) < 60  # sanity only


# ===========================================================================
# T3 — run_workflow_sandboxed_sync (the sync SANDBOX twin of run_workflow_sync)
# ===========================================================================
async def _seed_committed_wf(pg_session, wf_dict: dict) -> tuple[str, str, str]:
    """Seed tenant/user + a workflow whose CURRENT version content is ``wf_dict``,
    then COMMIT so an independent ``SyncWorkflowRepo`` short session (separate
    connection) can read it. Returns (tenant_hex, user_str, wf_id)."""
    import uuid as _uuid

    from sqlalchemy import text as _text

    from vibecanvas_api.storage.workflow_repo import WorkflowRepo

    t, u = _uuid.uuid4(), _uuid.uuid4()
    await pg_session.execute(
        _text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"), {"t": t})
    await pg_session.execute(
        _text("INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)"),
        {"u": u, "t": t, "e": f"{u.hex[:6]}@example.com"})
    await pg_session.execute(
        _text("SELECT set_config('app.tenant_id',:t,false)"), {"t": str(t)})
    repo = WorkflowRepo(pg_session, str(u))
    wf_id = (await repo.create_workflow(name="sandboxed-sync"))["wf_id"]
    await repo.commit(wf_id, wf_dict, note="seed pure wf")
    await pg_session.commit()
    return t.hex, str(u), wf_id


class _FsStore:
    """Minimal filesystem-style ObjectStore: ``materialize_prefix`` → a real dir
    (InMemory can't, which is why RunWorkspace's run_dir is None by default)."""

    def __init__(self, root):
        self.root = root

    def materialize_prefix(self, prefix):
        d = os.path.join(self.root, prefix)
        os.makedirs(d, exist_ok=True)
        return d


def _patch_fs_run_workspace(monkeypatch, root):
    """Point ``RunWorkspace``'s materialize seams at a real filesystem so
    ``run_dir`` is a real host dir (the gVisor provider needs a real
    run_dir; the default InMemory store yields None). Mirrors the
    ``_fs_object_store`` fixture in test_run_context_wiring.py."""
    from vibecanvas_api.services import vfs_run_context as rc_mod
    monkeypatch.setattr(rc_mod, "get_object_store", lambda: _FsStore(str(root)))


@gvisor
@pytest.mark.asyncio
async def test_run_workflow_sandboxed_sync_returns_outputs(
        pg_session, monkeypatch, tmp_path):
    """A PURE Start->Code->End workflow loaded from Postgres runs INSIDE gVisor
    via ``run_workflow_sandboxed_sync`` → (outputs dict, empty errors, float
    secs >= 0). Driven from a worker thread (no running loop), matching the real
    DBOS/``to_thread`` call context."""
    import asyncio as _asyncio

    from vibecanvas_api.services import workflow_runner as runner_mod

    tenant, user, wf_id = await _seed_committed_wf(pg_session, _trivial_pure_wf())
    _patch_fs_run_workspace(monkeypatch, tmp_path)

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id, inputs={"x": 1},
            tenant_id=tenant, user_id=user)

    outputs, errors, secs = await _asyncio.to_thread(_call)
    assert isinstance(outputs, dict), f"outputs not a dict: {outputs!r}"
    assert not errors, f"unexpected errors: {errors!r}"
    assert isinstance(secs, float) and secs >= 0.0, f"bad secs: {secs!r}"
    # The CodeNode increments x → __end__.v == 2 (proves the wf actually ran).
    assert outputs.get("__end__", {}).get("v") == 2, outputs


@gvisor
@pytest.mark.gvisor
@pytest.mark.asyncio
async def test_sandboxed_sync_workflow_persists_user_mount(
        pg_session, monkeypatch, tmp_path):
    """Deployment-style one-shot execution hydrates and writes back `/mount`."""
    import asyncio as _asyncio

    from vibecanvas_api.services import workflow_runner as runner_mod
    from vibecanvas_api.services.object_store import get_object_store
    from vibecanvas_api.services.user_mount_workspace import mount_scope_id
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.vfs_store import VfsRepo

    tenant, user, wf_id = await _seed_committed_wf(pg_session, _mount_io_wf())
    _patch_fs_run_workspace(monkeypatch, tmp_path)
    async with session_scope(tenant_id=tenant) as session:
        await VfsRepo(session, object_store=get_object_store()).upsert_artifact_bytes(
            wf_id=mount_scope_id(user),
            tenant=tenant,
            path="/mount/input.txt",
            data=b"deployment",
            content_type="text/plain",
        )

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id,
            inputs={},
            tenant_id=tenant,
            user_id=user,
        )

    outputs, errors, _seconds = await _asyncio.to_thread(_call)
    assert errors == {}, errors
    assert outputs["__end__"]["value"] == "deployment"

    async with session_scope(tenant_id=tenant) as session:
        persisted = await VfsRepo(
            session, object_store=get_object_store()
        ).read_bytes(
            wf_id=mount_scope_id(user),
            path="/mount/workflow-output.txt",
        )
    assert persisted == b"deployment-workflow"


@pytest.mark.asyncio
async def test_sandboxed_sync_raises_when_sandbox_unavailable(
        pg_session, monkeypatch, tmp_path):
    """Without runsc, ``get_sandbox_provider`` raises
    ``SandboxUnavailable`` → ``run_workflow_sandboxed_sync`` RE-RAISES a clear
    ``SandboxUnavailable`` (NO silent in-process fallback)."""
    import asyncio as _asyncio

    from vibecanvas_api.services import workflow_runner as runner_mod
    from vibecanvas_api.services.sandbox import SandboxUnavailable

    tenant, user, wf_id = await _seed_committed_wf(pg_session, _trivial_pure_wf())
    _patch_fs_run_workspace(monkeypatch, tmp_path)

    def _boom(*a, **kw):
        raise SandboxUnavailable("no runsc (test)")

    monkeypatch.setattr(runner_mod, "get_sandbox_provider", _boom)

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id, inputs={"x": 1},
            tenant_id=tenant, user_id=user)

    with pytest.raises(SandboxUnavailable) as ei:
        await _asyncio.to_thread(_call)
    assert "gVisor sandbox" in str(ei.value)
    assert "no in-process fallback" in str(ei.value)


@pytest.mark.asyncio
async def test_sandboxed_sync_raises_for_host_only_node(
        pg_session, monkeypatch, tmp_path):
    """A host-only node is rejected by the provider's pure-engine guard
    raises ``EngineNeedsHostNode`` → ``run_workflow_sandboxed_sync`` RE-RAISES a
    clear ``EngineNeedsHostNode`` (NO in-process fallback). No gVisor needed (the
    provider stub raises the guard)."""
    import asyncio as _asyncio

    from vibecanvas_api.services import workflow_runner as runner_mod
    from vibecanvas_api.services.sandbox import EngineNeedsHostNode
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider

    tenant, user, wf_id = await _seed_committed_wf(
        pg_session, _trivial_pure_wf())
    _patch_fs_run_workspace(monkeypatch, tmp_path)

    prov = RootlessGvisorProvider("/nonexistent/runsc")
    prov.run = lambda **kw: (_ for _ in ()).throw(
        AssertionError("sandbox must NOT launch for a host-only node"))
    prov.run_workflow = lambda **kw: (_ for _ in ()).throw(
        EngineNeedsHostNode("HostOnlyMadeUpNode"))
    monkeypatch.setattr(runner_mod, "get_sandbox_provider", lambda **kw: prov)

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id, inputs={"x": 1},
            tenant_id=tenant, user_id=user)

    with pytest.raises(EngineNeedsHostNode) as ei:
        await _asyncio.to_thread(_call)
    assert "not supported in the sandbox" in str(ei.value)
    assert "HostOnlyMadeUpNode" in str(ei.value)


@pytest.mark.asyncio
async def test_sandboxed_sync_raises_for_in_memory_store(
        pg_session, monkeypatch, tmp_path):
    """An in-memory object store yields ``run_dir is None``
    (its dict is process-local, can't be bind-mounted) → ``RuntimeError`` (NOT an
    in-process fallback). We stub the provider so it's "available" and force the
    workspace's run_dir to None (the default InMemory store behavior)."""
    import asyncio as _asyncio

    from vibecanvas_api.services import workflow_runner as runner_mod
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider

    tenant, user, wf_id = await _seed_committed_wf(
        pg_session, _trivial_pure_wf())
    # NOTE: deliberately do NOT patch the FS RunWorkspace → the default InMemory
    # object store can't materialize a dir, so build_run_context yields
    # run_dir=None (exactly the production in-memory-deploy case).

    prov = RootlessGvisorProvider("/nonexistent/runsc")
    prov.run_workflow = lambda **kw: (_ for _ in ()).throw(
        AssertionError("provider must NOT run when run_dir is None"))
    monkeypatch.setattr(runner_mod, "get_sandbox_provider", lambda **kw: prov)

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id, inputs={"x": 1},
            tenant_id=tenant, user_id=user)

    with pytest.raises(RuntimeError) as ei:
        await _asyncio.to_thread(_call)
    assert "real object store" in str(ei.value)
    assert "in-memory" in str(ei.value)


@pytest.mark.asyncio
async def test_sandboxed_sync_passes_allow_hosts_to_provider(
        pg_session, monkeypatch, tmp_path):
    """Plan-B B6: ``run_workflow_sandboxed_sync`` COMPUTES a per-run egress
    allowlist and threads it into ``provider.run_workflow(..., allow_hosts=...)``.

    No gVisor needed: a stub provider records the ``allow_hosts`` kwarg and
    returns a canned ``EngineRunResult``. We seed a workflow with a user-declared
    egress host so the computed allowlist is non-empty + deterministic (the pure
    wf references no LLM/MCP, so only the user host appears)."""
    import asyncio as _asyncio

    from vibecanvas_api.services import workflow_runner as runner_mod
    from vibecanvas_api.services.sandbox import EngineRunResult
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider

    wf = _trivial_pure_wf()
    wf["__meta__"] = {
        **wf.get("__meta__", {}),
        "settings": {"egress": {"allowed_hosts": ["api.example.com"]}},
    }
    tenant, user, wf_id = await _seed_committed_wf(pg_session, wf)
    _patch_fs_run_workspace(monkeypatch, tmp_path)

    captured: dict = {}

    prov = RootlessGvisorProvider("/nonexistent/runsc")

    def _fake_run_workflow(**kw):
        captured["allow_hosts"] = kw.get("allow_hosts")
        return EngineRunResult(
            final_outputs={"__end__": {"v": 2}}, error_dict={},
            execution_time=0.0)

    prov.run_workflow = _fake_run_workflow
    monkeypatch.setattr(runner_mod, "get_sandbox_provider", lambda **kw: prov)

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id, inputs={"x": 1},
            tenant_id=tenant, user_id=user)

    outputs, errors, secs = await _asyncio.to_thread(_call)
    assert outputs.get("__end__", {}).get("v") == 2
    # The provider was handed a NON-None allowlist containing the user-declared
    # host (pure wf → no LLM/MCP hosts, so exactly the user host).
    assert captured["allow_hosts"] is not None
    assert "api.example.com" in captured["allow_hosts"]


@gvisor
def test_workflow_runs_through_job_dispatcher(tmp_path):
    """The one-shot bundle now carries __exec__/job.json (kind=workflow); the
    in-sandbox run_job dispatches it to run_exec — final_outputs still correct,
    and job.json is present on the run-tier (proves the host wrote it)."""
    res = get_sandbox_provider().run_workflow(
        run_dir=str(tmp_path), workflow=_trivial_pure_wf(), inputs={"x": 1},
        run_id="rj", timeout=120.0,
    )
    assert res.error_dict == {}, f"engine errors: {res.error_dict}"
    assert res.final_outputs.get("__end__", {}).get("v") == 2
    assert (tmp_path / "__exec__" / "job.json").exists()


@gvisor
@pytest.mark.gvisor
def test_workflow_reads_and_writes_user_mount(tmp_path):
    """The real Workflow engine sees the same writable `/mount` bind."""
    run_dir = tmp_path / "run"
    mount_dir = tmp_path / "mount"
    run_dir.mkdir()
    mount_dir.mkdir()
    (mount_dir / "input.txt").write_text("shared", encoding="utf-8")

    result = get_sandbox_provider().run_workflow(
        run_dir=str(run_dir),
        workflow=_mount_io_wf(),
        inputs={},
        run_id="mount-io",
        mount_dir=str(mount_dir),
        timeout=120.0,
    )

    assert result.error_dict == {}, result.error_dict
    assert result.final_outputs["__end__"]["value"] == "shared"
    assert (mount_dir / "workflow-output.txt").read_text() == "shared-workflow"


# ===========================================================================
# Plan-B egress (B5) — FULL relay path E2E through gVisor (--network=none +
# in-sandbox proxy + host EgressBroker). Proves the proxy-mode wiring this slice
# added actually relays an outbound HTTP request through the allowlist broker.
# ===========================================================================
import http.server  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402


class _OKHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"EGRESS_OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence the test server's stderr noise
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get_wf(url: str) -> dict:
    """Start -> Code(urllib.request.urlopen(url)) -> End. CodeNode runs
    as normal Python in a worker subprocess (no pre-import jail).

    The worker is standard-library-only unless dependencies are supplied through
    ``VC_LIB_OVERLAY``, which this path does not bind. This test therefore uses
    ``urllib.request`` (STDLIB) instead of ``requests``; like requests it honors
    HTTP(S)_PROXY env, so in proxy mode every outbound still goes via the in-
    sandbox proxy → host broker — the egress-relay path stays fully exercised.
    Returns {'status': int, 'text': str} (status 0 / text=<err> on failure)."""
    code = (
        "import urllib.request\n"
        "def process_fn(inputs):\n"
        "    try:\n"
        f"        with urllib.request.urlopen({url!r}, timeout=15) as r:\n"
        "            body = r.read().decode('utf-8', 'replace')\n"
        "            return {'status': r.status, 'text': body}\n"
        "    except Exception as e:\n"
        "        return {'status': 0, 'text': repr(e)}\n"
    )
    return {
        "__meta__": {**_meta(), "settings": {"code_libraries": ["requests"]}},
        "node_1": _start(["node_2"]),
        "node_2": {
            "node_id": "node_2", "node_name": "fetch", "node_type": "CodeNode",
            "node_description": "http get via requests",
            "input_fields": {},
            "output_fields": {
                "status": {"type": "integer", "description": "http status"},
                "text": {"type": "string", "description": "body / error"},
            },
            "node_config": {"programming_language": "python", "process_fn": code},
            "children": ["node_3"],
        },
        "node_3": _end(
            {"status": {"type": "integer", "value": 0, "reference": "fetch.status"},
             "text": {"type": "string", "value": "", "reference": "fetch.text"}},
            {"status": {"type": "integer", "description": "http status"},
             "text": {"type": "string", "description": "body"}},
        ),
    }


@gvisor
def test_proxy_mode_denies_loopback_even_when_requested(tmp_path, monkeypatch):
    """A caller-provided host ceiling cannot override the SSRF boundary.

    The sandbox has ``--network=none`` and the host broker must reject loopback
    even when ``allow_hosts`` contains it; otherwise a workflow could reach API
    control-plane listeners or host-only credentials.
    """
    from vibecanvas_api.config import config

    monkeypatch.setattr(config, "sandbox_egress_mode", "proxy")

    # Host-local mock HTTP server on 127.0.0.1:<port> (the broker dials it on the
    # HOST network; the sandbox itself has --network=none).
    port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), _OKHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"
    try:
        prov = get_sandbox_provider()

        run_dir = tmp_path / "loopback-denied"
        run_dir.mkdir()
        result = prov.run_workflow(
            run_dir=str(run_dir), workflow=_http_get_wf(url), inputs={},
            run_id="egr-loopback-denied", timeout=120.0,
            allow_hosts={"127.0.0.1"},
        )
        assert result.error_dict == {}, (
            f"engine errors: {result.error_dict}\n"
            f"stderr={getattr(result.sandbox, 'stderr', '')[-1500:]}"
        )
        output = result.final_outputs.get("__end__", {})
        assert output.get("status") != 200, (
            f"loopback egress unexpectedly succeeded: {output}"
        )
        assert "EGRESS_OK" not in (output.get("text") or ""), output
    finally:
        httpd.shutdown()
        httpd.server_close()


# ===========================================================================
# End-to-end CodeNode dependency-overlay coverage under real
# gVisor, proving the whole chain:
#
#   declare a 3rd-party lib in Workflow Settings (``__meta__.settings.
#   code_requirements``) → built into a content-addressed overlay
#   ``ensure_overlay``, real host pip ``--only-binary=:all:``) → the runner
#   provisions and binds it at /opt/lib-overlay → the in-sandbox
#   CodeNode worker (PYTHONPATH=VC_LIB_OVERLAY, ``-S`` = NO host site-packages)
#   imports it.
#
# We use ``six==1.16.0`` — a tiny, pure-Python wheel. The host has six 1.17.0
# installed, so:
#   * test 1 asserts the imported version is ``1.16.0`` (the OVERLAY's pin), NOT
#     the host's 1.17.0 — a hard proof it came from the overlay, not a leak.
#   * test 2 (NO declaration) asserts the import FAILS even though six IS on the
#     host — proving ``-S`` + overlay-only actually hides the host site-packages.
# ===========================================================================
# ``socket`` is already imported at module top (egress mock-server section).


def _pypi_reachable() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=4).close()
        return True
    except OSError:
        return False


def _codenode_six_wf(*, code_requirements: "str | None") -> dict:
    """Start -> Code(``import six``; return version + ok) -> End.

    ``import six`` is INSIDE ``process_fn`` so a missing module surfaces as a
    NODE-execution error (filed under ``error_dict[node_id]``) rather than an
    exec-time top-level error — exactly the clean per-node failure 3b proves.

    ``code_requirements`` (when non-empty) is written to
    ``__meta__.settings.code_requirements`` so the runner's L3 path provisions
    the overlay; when ``None`` no overlay is provisioned (L2: stdlib-only worker
    → the host's six is invisible under ``-S``)."""
    code = (
        "def process_fn(inputs):\n"
        "    import six\n"
        "    return {'ver': six.__version__, 'ok': hasattr(six, 'moves')}\n"
    )
    meta = dict(_meta())
    if code_requirements:
        meta["settings"] = {"code_requirements": code_requirements}
    return {
        "__meta__": meta,
        "node_1": _start(["node_2"]),
        "node_2": {
            "node_id": "node_2",
            "node_name": "needs_six",
            "node_type": "CodeNode",
            "node_description": "import six (3rd-party, declared via overlay)",
            "input_fields": {},
            "output_fields": {
                "ver": {"type": "string", "description": "six version"},
                "ok": {"type": "boolean", "description": "six.moves present"},
            },
            "node_config": {"programming_language": "python", "process_fn": code},
            "children": ["node_3"],
        },
        "node_3": _end(
            {"ver": {"type": "string", "value": "", "reference": "needs_six.ver"},
             "ok": {"type": "boolean", "value": False, "reference": "needs_six.ok"}},
            {"ver": {"type": "string", "description": "six version"},
             "ok": {"type": "boolean", "description": "six.moves present"}},
        ),
    }


def _codenode_stdlib_wf() -> dict:
    """Start -> Code(``import json, statistics``; pure stdlib) -> End, with NO
    ``code_requirements`` — the L2 path (stdlib always available, no overlay)."""
    code = (
        "def process_fn(inputs):\n"
        "    import json, statistics\n"
        "    return {'med': statistics.median([1, 3, 2]),\n"
        "            'js': json.dumps({'a': 1})}\n"
    )
    return {
        "__meta__": _meta(),
        "node_1": _start(["node_2"]),
        "node_2": {
            "node_id": "node_2",
            "node_name": "stdlib",
            "node_type": "CodeNode",
            "node_description": "import json+statistics (stdlib only)",
            "input_fields": {},
            "output_fields": {
                "med": {"type": "number", "description": "median"},
                "js": {"type": "string", "description": "json dump"},
            },
            "node_config": {"programming_language": "python", "process_fn": code},
            "children": ["node_3"],
        },
        "node_3": _end(
            {"med": {"type": "number", "value": 0, "reference": "stdlib.med"},
             "js": {"type": "string", "value": "", "reference": "stdlib.js"}},
            {"med": {"type": "number", "description": "median"},
             "js": {"type": "string", "description": "json dump"}},
        ),
    }


@gvisor
@pytest.mark.skipif(
    not _pypi_reachable(),
    reason="PyPI unreachable — the real ``six`` overlay build needs network",
)
@pytest.mark.asyncio
async def test_codenode_declared_lib_imports_from_overlay(
        pg_session, pg_engine, monkeypatch, tmp_path):
    """PAYOFF (gVisor + real pip): a workflow that declares ``six==1.16.0`` runs
    INSIDE the sandbox and its CodeNode imports six FROM THE OVERLAY.

    The whole chain is exercised: a REAL ``ensure_overlay`` (host pip
    ``--only-binary=:all:`` into a content-addressed dir) builds the overlay, the
    provider binds it at /opt/lib-overlay + sets ``VC_LIB_OVERLAY``, and the
    ``-S`` CodeNode worker imports six from it. The imported version must be
    ``1.16.0`` (the OVERLAY's pin) — NOT the host's 1.17.0 — which is the hard
    proof it came from the overlay and not a host-site-packages leak.

    The DB write of ``ensure_overlay`` (admin session) is bound to the test's
    event loop, but the runner provisions via ``asyncio.run`` from a worker
    thread (cross-loop). So we BUILD the overlay HERE (in-loop, real pip,
    cached) and hand the resulting ``EnsureResult`` back from a patched
    ``runner_mod.ensure_overlay`` — the build + bind + import-from-overlay are
    all REAL; only the (already-proven elsewhere) DB cache-lookup is bypassed."""
    from unittest.mock import AsyncMock

    from vibecanvas_api.config import config
    from vibecanvas_api.services.env.overlay_builder import ensure_overlay
    from vibecanvas_api.services import workflow_runner as runner_mod
    from vibecanvas_api.storage import db as db_mod

    # Point the overlay builder at a PERSISTENT tmp root + the test admin engine,
    # then build six==1.16.0 for real (in this loop). Mirrors test_overlay_builder.
    monkeypatch.setattr(config, "lib_overlay_root", str(tmp_path / "lib-overlay"))
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)
    built = await ensure_overlay("six==1.16.0")
    assert built.status == "ready", f"overlay build failed: {built.error_log}"
    assert built.path and os.path.isdir(built.path), built
    # Sanity: the overlay really contains six (host-side, before the run).
    assert os.path.exists(os.path.join(built.path, "six.py")), os.listdir(built.path)

    # Hand the pre-built result back from the runner's ensure_overlay (avoids the
    # cross-loop DB hit; the bind + import-from-overlay below are still real).
    monkeypatch.setattr(
        runner_mod, "ensure_overlay",
        AsyncMock(return_value=built))

    tenant, user, wf_id = await _seed_committed_wf(
        pg_session, _codenode_six_wf(code_requirements="six==1.16.0"))
    _patch_fs_run_workspace(monkeypatch, tmp_path)

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id, inputs={}, tenant_id=tenant, user_id=user)

    import asyncio as _asyncio
    outputs, errors, secs = await _asyncio.to_thread(_call)
    assert not errors, f"CodeNode failed to import six from overlay: {errors!r}"
    end = outputs.get("__end__", {})
    assert end.get("ok") is True, f"six imported but incomplete: {end!r}"
    # THE PROOF: the version is the overlay's pin 1.16.0, not the host's 1.17.0.
    assert end.get("ver") == "1.16.0", (
        f"expected six 1.16.0 from the overlay, got {end.get('ver')!r} "
        "(if this is the host version the overlay was NOT the import source)")


@gvisor
@pytest.mark.asyncio
async def test_codenode_can_use_packages_from_the_base_sandbox(
        pg_session, monkeypatch, tmp_path):
    """The stable base sandbox exposes its reviewed Python package set.

    ``code_requirements`` is only for Workflow-specific packages or version
    overrides; it is not required for packages already present in the base
    Runtime. The sandbox still receives no provider/database credentials."""
    import asyncio as _asyncio

    from vibecanvas_api.services import workflow_runner as runner_mod

    tenant, user, wf_id = await _seed_committed_wf(
        pg_session, _codenode_six_wf(code_requirements=None))
    _patch_fs_run_workspace(monkeypatch, tmp_path)

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id, inputs={}, tenant_id=tenant, user_id=user)

    outputs, errors, secs = await _asyncio.to_thread(_call)
    assert not errors, f"base package import failed: {errors!r}"
    end = outputs.get("__end__", {})
    assert end.get("ok") is True
    assert end.get("ver") == "1.17.0"


@gvisor
@pytest.mark.asyncio
async def test_codenode_stdlib_always_works(
        pg_session, monkeypatch, tmp_path):
    """A CodeNode importing ONLY stdlib (json, statistics) with NO
    ``code_requirements`` runs successfully — stdlib is always available to the
    ``-S`` worker (no overlay needed; the L2 path). No network."""
    import asyncio as _asyncio

    from vibecanvas_api.services import workflow_runner as runner_mod

    tenant, user, wf_id = await _seed_committed_wf(
        pg_session, _codenode_stdlib_wf())
    _patch_fs_run_workspace(monkeypatch, tmp_path)

    def _call():
        return runner_mod.run_workflow_sandboxed_sync(
            workflow_id=wf_id, inputs={}, tenant_id=tenant, user_id=user)

    outputs, errors, secs = await _asyncio.to_thread(_call)
    assert not errors, f"stdlib CodeNode unexpectedly failed: {errors!r}"
    end = outputs.get("__end__", {})
    assert end.get("med") == 2, f"statistics.median wrong: {end!r}"
    assert end.get("js") == '{"a": 1}', f"json.dumps wrong: {end!r}"
