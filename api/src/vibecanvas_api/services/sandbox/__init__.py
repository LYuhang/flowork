"""OS-sandbox provider package (RE-6 P1).

API-side bubblewrap/gVisor sandbox — distinct from the in-process
``engine/.../sandbox.py``. The engine never imports this package.

Public surface: ``SandboxProvider`` / ``SandboxResult`` / ``SandboxUnavailable``
(interface), ``build_oci_config`` (bundle builder), and ``get_sandbox_provider``
(the configured backend resolver).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

from .gvisor import (
    BusRunHandle,
    ColdBootProvider,
    EngineNeedsHostNode,
    EngineRunResult,
    RootfulGvisorProvider,
    RootlessGvisorProvider,
    ServeSnapshot,
    ServeHandle,
    _workflow_python_binds,
    _workflow_python_env,
    build_oci_config,
)
from .bubblewrap import BubblewrapProvider
# Host↔sandbox UDS message bus with a per-run socket.
from .bus_broker import (
    BusBroker,
    IN_SANDBOX_BUS_SOCK,
    socket_path_for,
)
from .provider import SandboxProvider, SandboxResult, SandboxUnavailable
from .contracts import (
    SandboxCapabilities,
    SandboxClient,
    SandboxRef,
    SandboxScope,
    SandboxScopeKind,
    SandboxSpec,
)
from .warm import WarmGvisorPool
from .warm_manager import PoolCapacityExceeded, WarmPoolManager
# Credential-free workflow admission. Host/API nodes use brokers and never
# receive a database-role exception inside gVisor.
from .workflow_guard import (
    SANDBOX_RUNNABLE_NODE_TYPES,
    classify_workflow,
)

__all__ = [
    "SandboxResult",
    "SandboxProvider",
    "SandboxUnavailable",
    "get_sandbox_provider",
    "build_oci_config",
    "EngineNeedsHostNode",
    "EngineRunResult",
    "ColdBootProvider",
    "RootlessGvisorProvider",
    "RootfulGvisorProvider",
    "BubblewrapProvider",
    "ServeSnapshot",
    "ServeHandle",
    # Host↔sandbox UDS bus.
    "BusRunHandle",
    "BusBroker",
    "IN_SANDBOX_BUS_SOCK",
    "socket_path_for",
    "WarmGvisorPool",
    "WarmPoolManager",
    "PoolCapacityExceeded",
    "_gvisor_runnable",
    "_resolve_runsc",
    # Credential-free workflow admission
    "SANDBOX_RUNNABLE_NODE_TYPES",
    "classify_workflow",
    "SandboxCapabilities",
    "SandboxClient",
    "SandboxRef",
    "SandboxScope",
    "SandboxScopeKind",
    "SandboxSpec",
]


def _resolve_runsc() -> str | None:
    """Locate the ``runsc`` binary: env ``RUNSC_PATH`` → ``settings.runsc_path``
    → ``shutil.which("runsc")`` → ``None``. Not invoked at import (a bootstrap
    script fetches runsc; see ``scripts/get_runsc.sh``)."""
    env_path = os.environ.get("RUNSC_PATH")
    if env_path:
        return env_path
    try:
        from vibecanvas_api.config import config

        if getattr(config, "runsc_path", None):
            return config.runsc_path
    except Exception:
        pass
    return shutil.which("runsc")


def _resolve_bwrap() -> str | None:
    """Locate the ``bwrap`` binary: env ``BWRAP_PATH`` → ``config.bwrap_path``
    → ``shutil.which("bwrap")`` → ``None``. Mirrors ``_resolve_runsc``; unlike
    ``runsc`` this is an ordinary distro package (``apt-get install
    bubblewrap``), not a pinned fetched binary."""
    env_path = os.environ.get("BWRAP_PATH")
    if env_path:
        return env_path
    try:
        from vibecanvas_api.config import config

        if getattr(config, "bwrap_path", None):
            return config.bwrap_path
    except Exception:
        pass
    return shutil.which("bwrap")


_BUBBLEWRAP_RUNNABLE: bool | None = None
_BUBBLEWRAP_RUNNABLE_PROFILE: tuple[str | None, int] | None = None


def _bubblewrap_runnable() -> bool:
    """Return True iff bubblewrap can actually boot a sandbox here.

    Same marker-round-trip shape as ``_gvisor_runnable``: a bare ``bwrap true``
    can succeed on hosts where unprivileged user namespaces are unavailable
    only for the fuller mount profile the application actually uses, so the
    probe exercises the real ``BubblewrapProvider.run`` path.
    """
    global _BUBBLEWRAP_RUNNABLE, _BUBBLEWRAP_RUNNABLE_PROFILE
    bwrap = _resolve_bwrap()
    profile = (bwrap, os.geteuid())
    if _BUBBLEWRAP_RUNNABLE is not None and _BUBBLEWRAP_RUNNABLE_PROFILE == profile:
        return _BUBBLEWRAP_RUNNABLE

    runnable = False
    if bwrap:
        try:
            with tempfile.TemporaryDirectory(prefix="vc-bwrap-probe-") as probe_root:
                marker_name = ".capability-probe"
                run_dir = os.path.join(probe_root, "channel")
                os.makedirs(run_dir, exist_ok=True)
                result = BubblewrapProvider(bwrap).run(
                    run_dir=run_dir,
                    command=[
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import vibecanvas_engine; "
                        f"Path('/run/{marker_name}').write_text("
                        "'flowork-bubblewrap-ready', encoding='utf-8')",
                    ],
                    env=_workflow_python_env(),
                    network="none",
                    timeout=15.0,
                    extra_ro_binds=_workflow_python_binds(),
                )
                marker_path = os.path.join(run_dir, marker_name)
                marker_contents = ""
                if result.exit_code == 0 and os.path.isfile(marker_path):
                    with open(marker_path, encoding="utf-8") as marker:
                        marker_contents = marker.read()
                runnable = marker_contents == "flowork-bubblewrap-ready"
        except Exception:
            runnable = False

    _BUBBLEWRAP_RUNNABLE = runnable
    _BUBBLEWRAP_RUNNABLE_PROFILE = profile
    return runnable


# Cached once per process: the real boot smoke is expensive (~hundreds of ms)
# and its result is environment-static for the lifetime of the process.
_GVISOR_RUNNABLE: bool | None = None
_GVISOR_RUNNABLE_PROFILE: tuple[str | None, bool, int, str] | None = None


def _gvisor_runnable() -> bool:
    """Return True iff the configured gVisor profile can actually boot here.

    The probe uses the same OCI bundle builder and writable bind-mount path as a
    real one-shot sandbox. A bare ``runsc do true`` is insufficient: some hosted
    runners can execute that command but fail or hang once the application adds
    its real mount profile. The marker round-trip therefore proves both boot and
    host/sandbox file-channel behavior before guarded integration tests run.

    The result is cached in a module global (computed once). Any failure —
    unresolved runsc, nonzero exit, missing marker, timeout, or unexpected
    exception — returns False (fail-closed).
    """
    global _GVISOR_RUNNABLE, _GVISOR_RUNNABLE_PROFILE
    runsc = _resolve_runsc()
    from vibecanvas_api.config import config

    rootful = bool(getattr(config, "sandbox_rootful", False))
    platform = str(getattr(config, "sandbox_gvisor_platform", "systrap"))
    profile = (runsc, rootful, os.geteuid(), platform)
    if _GVISOR_RUNNABLE is not None and _GVISOR_RUNNABLE_PROFILE == profile:
        return _GVISOR_RUNNABLE

    runnable = False
    if runsc:
        try:
            if rootful and os.geteuid() != 0:
                _GVISOR_RUNNABLE = False
                _GVISOR_RUNNABLE_PROFILE = profile
                return False
            provider_cls = (
                RootfulGvisorProvider if rootful else RootlessGvisorProvider
            )
            with tempfile.TemporaryDirectory(prefix="vc-gvisor-probe-") as probe_root:
                marker_name = ".capability-probe"
                run_dir = os.path.join(probe_root, "channel")
                bus_socket = os.path.join(probe_root, "bus", "probe.sock")
                workspace_binds = []
                for destination in ("/data", "/memory", "/logs", "/chats", "/mount"):
                    source = os.path.join(probe_root, destination.lstrip("/"))
                    os.makedirs(source, exist_ok=True)
                    workspace_binds.append((destination, source))
                os.makedirs(run_dir, exist_ok=True)
                result = provider_cls(runsc).run(
                    run_dir=run_dir,
                    command=[
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import vibecanvas_engine; "
                        f"Path('/run/{marker_name}').write_text("
                        "'flowork-gvisor-ready', encoding='utf-8')",
                    ],
                    env=_workflow_python_env(),
                    network="none",
                    timeout=15.0,
                    extra_ro_binds=_workflow_python_binds(),
                    extra_rw_binds=workspace_binds,
                    bus_socket=bus_socket,
                )
                marker_path = os.path.join(run_dir, marker_name)
                marker_contents = ""
                if result.exit_code == 0 and os.path.isfile(marker_path):
                    with open(marker_path, encoding="utf-8") as marker:
                        marker_contents = marker.read()
                runnable = marker_contents == "flowork-gvisor-ready"
        except Exception:
            runnable = False

    _GVISOR_RUNNABLE = runnable
    _GVISOR_RUNNABLE_PROFILE = profile
    return runnable


def get_sandbox_provider(*, trust: str = "trusted") -> SandboxProvider:
    """Resolve an OS-sandbox provider, or raise ``SandboxUnavailable``.

    ``SANDBOX_RUNTIME`` selects bubblewrap by default or explicit gVisor.
    ``SANDBOX_TYPE`` selects the privilege/lifecycle profile. Missing provider
    binaries fail explicitly; this function never switches backends silently.
    """
    # TODO RE-6 P3: trust×config policy + prod startup-assert + ManagedApiProvider
    from vibecanvas_api.config import config

    if str(getattr(config, "sandbox_runtime", "bubblewrap")) == "bubblewrap":
        bwrap_path = _resolve_bwrap()
        if not bwrap_path:
            raise SandboxUnavailable(
                "no OS-sandbox provider available: bwrap not found "
                "(set BWRAP_PATH / config.bwrap_path or install bubblewrap)"
            )
        return BubblewrapProvider(bwrap_path)

    path = _resolve_runsc()
    if not path:
        raise SandboxUnavailable(
            "no OS-sandbox provider available: runsc not found "
            "(set RUNSC_PATH / config.runsc_path or install runsc on PATH)"
        )

    if bool(getattr(config, "sandbox_rootful", False)):
        if os.geteuid() != 0:
            raise SandboxUnavailable(
                "rootful SANDBOX_TYPE requires sandboxd to run as uid 0"
            )
        return RootfulGvisorProvider(path)
    return RootlessGvisorProvider(path)
