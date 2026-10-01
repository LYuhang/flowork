"""bubblewrap (bwrap) OS-sandbox provider.

A second, lighter-weight alternative to the gVisor providers in ``gvisor.py``.
bubblewrap uses plain Linux namespaces + seccomp — it does not emulate a
kernel, so the sandboxed process's own memory shows up as ordinary process
RSS instead of being hosted inside a userspace-kernel Sentry. It is also
rootless-native: unprivileged user namespaces are its whole design point (it
is what backs Flatpak), so it needs no ``/dev/kvm`` and no CAP_SYS_ADMIN.

The trade-off is a weaker isolation boundary than gVisor (it shares the host
kernel; there is no syscall-emulation layer). It is also weaker than a
"stock" bubblewrap sandbox on this deployment specifically: mounting a fresh
``/proc`` fails with EPERM on this host's own (nested/restricted) mount
namespace, so ``/proc`` is bind-mounted from the host read-only instead —
see the comment in ``_build_argv`` — which gives up PID-namespace hiding.
Every other unshared namespace (mount, network, IPC, UTS) is unaffected.

``run_serve``/``stop_serve``
ARE implemented — ``manager.py``'s "coldboot" resident mode (the default;
see its module docstring) uses exactly these two to boot/tear down the one
long-lived worker that serves every agent-visible shell/file-tool call for a
Chat, via ``warm.py``'s ``WarmGvisorPool``, so a provider that stubbed them
out could not run a real Chat. bubblewrap has no checkpoint/restore
equivalent, though: ``checkpoint_serve`` is a documented no-op and
``restore_serve`` just re-boots via ``run_serve`` (there is nothing to
restore from) — rootless gVisor itself already refuses both outright for the
same reason (``RootlessGvisorProvider.checkpoint_serve`` raises
``RuntimeError``), so no caller on the rootless path ever depended on a real
snapshot to begin with. ``launch_agent_runtime_bus``/``launch_workflow_bus``
(sandboxing the Agent Runtime process itself — the ``__projectws_v1_...``
sandboxed Project path, a separate mechanism from both the file/shell-tool warm
worker above and the *unsandboxed* ``codex.py``/``create_codex_app_server``
host-direct path some Chats use) are also implemented, reusing
``_build_workflow_invocation``/``_sandbox_egress_setup`` inherited from
``RootlessGvisorProvider`` (both are already provider-agnostic — they only
write job files and set up a broker, never call gVisor/OCI machinery). Their
snapshot-restore branch raises, matching rootless gVisor's own behavior for
the identical reason as ``checkpoint_serve``.
Every one-shot method that only calls ``self.run()``
underneath (``run_code``, ``run_node``, ``run_mcp_probe``, ``run_workflow``,
plus the shared ``_workflow_python_binds``/``_workflow_python_env`` helpers)
is inherited unchanged from ``RootlessGvisorProvider``.
"""

from __future__ import annotations

import os
import subprocess
import signal
import sys
import time
import uuid

from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox.bus_broker import IN_SANDBOX_BUS_DIR
from vibecanvas_api.services.sandbox.gvisor import (
    _DEFAULT_PATH,
    _EGRESS_PROXY_PORT,
    _EGRESS_PORT_ENV,
    _EGRESS_SOCK_ENV,
    _HOST_RO_BINDS,
    _BUS_SOCK_ENV,
    _LIB_OVERLAY_ENV,
    _resolve_network,
    _workflow_python_binds,
    _workflow_python_env,
    BusRunHandle,
    IN_SANDBOX_BUS_SOCK,
    IN_SANDBOX_EGRESS_DIR,
    IN_SANDBOX_EGRESS_SOCK,
    IN_SANDBOX_LIB_OVERLAY,
    RootlessGvisorProvider,
    ServeHandle,
    ServeSnapshot,
)
from vibecanvas_api.services.sandbox.provider import SandboxResult


class BubblewrapProvider(RootlessGvisorProvider):
    """One-shot bubblewrap command runner (boot -> run -> capture -> teardown).

    Subclasses ``RootlessGvisorProvider`` purely to inherit its one-shot job
    wrappers (``run_code`` etc.) and shared Python-runtime bind helpers; it
    does not call any of the parent's gVisor/OCI-bundle machinery.
    """

    def __init__(self, bwrap_path: str):
        # Deliberately does not call RootlessGvisorProvider.__init__: this
        # provider has no runsc path/rootless flag to set, only bwrap_path.
        self._bwrap = bwrap_path

    def run(
        self,
        *,
        run_dir: str,
        command: list[str],
        env: dict | None = None,
        network: "str | None" = None,
        timeout: float = 60.0,
        extra_ro_binds: "list[str] | tuple[str, ...]" = (),
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        bus_socket: str | None = None,
        egress_socket: str | None = None,
        lib_overlay: str | None = None,
        run_mount: str = "/run",
        cancel_event=None,
        extra_ro_dest_binds: list[tuple[str, str]] | None = None,
    ) -> SandboxResult:
        rw_binds: list[tuple[str, str]] = [(run_mount, run_dir)]
        for dest, source in extra_rw_binds or []:
            os.makedirs(source, exist_ok=True)
            rw_binds.append((dest, source))

        env = dict(env or {})
        if bus_socket is not None:
            bus_host_dir = os.path.dirname(bus_socket)
            os.makedirs(bus_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_BUS_DIR, bus_host_dir))
            env[_BUS_SOCK_ENV] = IN_SANDBOX_BUS_SOCK
        if egress_socket is not None:
            egress_host_dir = os.path.dirname(egress_socket)
            os.makedirs(egress_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env[_EGRESS_SOCK_ENV] = IN_SANDBOX_EGRESS_SOCK
            env[_EGRESS_PORT_ENV] = str(_EGRESS_PROXY_PORT)
        ro_dest_binds: list[tuple[str, str]] = list(extra_ro_dest_binds or [])
        if lib_overlay is not None:
            ro_dest_binds.append((IN_SANDBOX_LIB_OVERLAY, lib_overlay))
            env[_LIB_OVERLAY_ENV] = IN_SANDBOX_LIB_OVERLAY

        env.setdefault("PATH", _DEFAULT_PATH)
        env.setdefault("PYTHONUNBUFFERED", "1")

        argv = self._build_argv(
            command=command,
            env=env,
            network=network,
            rw_binds=rw_binds,
            extra_ro_binds=extra_ro_binds,
            ro_dest_binds=ro_dest_binds,
        )

        from .process_wait import communicate

        started = time.monotonic()
        # Same process-group timeout/kill contract as
        # ``RootlessGvisorProvider.run`` (gvisor.py) — bwrap has no bundle or
        # container-state directory to tear down afterward, only the process.
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = communicate(proc, timeout=timeout, cancel_event=cancel_event)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            stdout, stderr = proc.communicate()
            exit_code = -signal.SIGKILL
        duration_s = time.monotonic() - started
        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_s=duration_s,
        )

    def _build_argv(
        self,
        *,
        command: list[str],
        env: dict[str, str],
        network: "str | None",
        rw_binds: list[tuple[str, str]],
        extra_ro_binds: "list[str] | tuple[str, ...]",
        ro_dest_binds: list[tuple[str, str]],
    ) -> list[str]:
        argv = [self._bwrap, "--die-with-parent", "--unshare-all"]
        if _resolve_network(network) != "none":
            argv.append("--share-net")
        # An empty tmpfs root; every visible path below is an explicit mount,
        # mirroring the identity-bind allowlist ``build_oci_config`` uses for
        # the OCI rootfs (gvisor.py's ``_HOST_RO_BINDS``).
        argv += ["--tmpfs", "/"]

        mounts: list[tuple[str, list[str]]] = []
        identity_roots: list[str] = []
        for host_dir in _HOST_RO_BINDS:
            if os.path.exists(host_dir):
                mounts.append((host_dir, ["--ro-bind", host_dir, host_dir]))
                identity_roots.append(host_dir)
        for host_dir in extra_ro_binds:
            normalized = os.path.abspath(host_dir) if host_dir else ""
            if (
                normalized
                and os.path.isabs(host_dir)
                and os.path.isdir(normalized)
                and not any(
                    normalized == root
                    or os.path.commonpath((normalized, root)) == root
                    for root in identity_roots
                )
            ):
                mounts.append((normalized, ["--ro-bind", normalized, normalized]))
                identity_roots.append(normalized)
        for dest, source in ro_dest_binds:
            if source and os.path.isdir(source):
                mounts.append((dest, ["--ro-bind", source, dest]))

        # Mounting a FRESH procfs (bwrap's ``--proc``) is refused with EPERM on
        # hosts whose own mount namespace is itself nested/restricted (verified
        # against this deployment host: plain ``unshare --mount-proc`` fails
        # there too, so this is a host/kernel policy, not a bwrap bug). Bind
        # the host's existing ``/proc`` read-only instead — the documented
        # bwrap workaround for nested containers. This trades away PID-
        # namespace hiding (the sandboxed process can see host process
        # listings via the bound /proc) for functioning at all here; every
        # other namespace (mount, network, IPC, UTS) unshared by
        # ``--unshare-all`` is unaffected.
        mounts.extend([
            ("/proc", ["--ro-bind", "/proc", "/proc"]),
            ("/dev", ["--dev", "/dev"]),
        ])
        rw_destinations = {dest for dest, _source in rw_binds}
        if "/tmp" not in rw_destinations:
            mounts.append(("/tmp", ["--tmpfs", "/tmp"]))
        for dest, source in rw_binds:
            mounts.append((dest, ["--bind", source, dest]))
        # Match OCI ordering: a late /tmp tmpfs or writable channel must not
        # hide an explicitly bound runtime/package under /tmp. Keep every
        # mount's original read/write options and stable ordering among peers.
        mounts.sort(key=lambda mount: os.path.normpath(mount[0]).count("/"))
        for _destination, arguments in mounts:
            argv += arguments
        argv += ["--chdir", rw_binds[0][0]]

        argv.append("--clearenv")
        for key, value in env.items():
            argv += ["--setenv", key, str(value)]

        argv.append("--")
        argv += list(command)
        return argv

    def run_serve(
        self,
        *,
        runs_root: str,
        work_dir: str,
        ro_binds: "list[str] | tuple[str, ...]" = (),
        env: dict | None = None,
        network: "str | None" = None,
        command: "list[str] | None" = None,
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        extra_ro_dest_binds: "list[tuple[str, str]] | None" = None,
        egress_socket: str | None = None,
        bootstrap_data: dict | None = None,
    ) -> ServeHandle:
        """Boot the long-lived warm worker WarmGvisorPool serves jobs through.

        Same contract and default command as
        ``RootlessGvisorProvider.run_serve`` (gvisor.py) — the engine's
        ``sandbox_entry serve`` loop is a plain Python entrypoint, agnostic to
        which sandbox backend launched it. Unlike ``run()``, this returns
        immediately without waiting; ``stop_serve`` owns teardown.
        """
        if command is None:
            command = [
                sys.executable,
                "-m",
                "vibecanvas_engine.sandbox_entry",
                "serve",
                "/work",
                "/runs",
            ]
        rw_binds = [("/runs", runs_root), ("/work", work_dir)]
        rw_binds += list(extra_rw_binds or [])
        env = dict(env or {})
        if egress_socket is not None:
            egress_host_dir = os.path.dirname(egress_socket)
            os.makedirs(egress_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env[_EGRESS_SOCK_ENV] = IN_SANDBOX_EGRESS_SOCK
            env[_EGRESS_PORT_ENV] = str(_EGRESS_PROXY_PORT)
        env.setdefault("PATH", _DEFAULT_PATH)
        env.setdefault("PYTHONUNBUFFERED", "1")

        argv = self._build_argv(
            command=command,
            env=env,
            network=network,
            rw_binds=rw_binds,
            extra_ro_binds=ro_binds,
            ro_dest_binds=list(extra_ro_dest_binds or []),
        )
        # start_new_session so stop_serve can kill the whole process group,
        # matching RootlessGvisorProvider.run_serve.
        resource_group = getattr(self, 'resource_group', None)
        if resource_group is not None:
            argv = resource_group.wrap_command(argv)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if bootstrap_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if bootstrap_data is not None:
            # One-shot private bootstrap pipe: control credentials must not be
            # placed in argv, environment, or the shared filesystem.
            try:
                import json
                proc.stdin.write(json.dumps(bootstrap_data) + "\n")
                proc.stdin.flush()
            except BaseException:
                # Bootstrap failures must not leave an unowned resident process.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
                raise
            finally:
                proc.stdin.close()
        # bundle_dir/state_root/run_id exist only to satisfy the shared
        # ServeHandle shape; bubblewrap has no bundle or OCI state directory
        # to track, and warm.py only ever reads handle.proc (verified against
        # every ServeHandle use in warm.py).
        return ServeHandle(
            proc=proc,
            bundle_dir="",
            state_root="",
            run_id=uuid.uuid4().hex,
            network=network,
        )

    def stop_serve(self, handle: ServeHandle) -> None:
        """Tear down a warm worker. Best-effort + idempotent, mirroring

        ``RootlessGvisorProvider.stop_serve`` minus the OCI
        ``runsc delete``/bundle-dir cleanup it has no equivalent of.
        """
        try:
            process_group = os.getpgid(handle.proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            process_group = None
        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            handle.proc.wait(timeout=2.0)
        except (subprocess.TimeoutExpired, ChildProcessError, OSError):
            pass

    def checkpoint_serve(
        self,
        handle: ServeHandle,
        *,
        image_dir: str,
        timeout: float | None = None,
    ) -> None:
        """No-op: bubblewrap has no checkpoint/restore.

        Rootless gVisor already refuses this same call outright
        (``RuntimeError("checkpoint/restore is unavailable in rootless
        mode")``), so nothing on the rootless path depends on a real
        snapshot existing afterward. No-op (rather than raising) lets the
        resident-session TTL/eviction path in manager.py treat every
        provider identically before it calls ``stop_serve``.
        """
        return None

    def restore_serve(
        self,
        *,
        snapshot: ServeSnapshot,
        runs_root: str,
        work_dir: str,
        ro_binds: "list[str] | tuple[str, ...]" = (),
        env: dict | None = None,
        network: "str | None" = None,
        command: "list[str] | None" = None,
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        extra_ro_dest_binds: "list[tuple[str, str]] | None" = None,
        egress_socket: str | None = None,
    ) -> ServeHandle:
        """Nothing was checkpointed (see ``checkpoint_serve``): "restoring" is

        just booting a fresh worker. ``snapshot`` is accepted for interface
        parity with ``RootlessGvisorProvider.restore_serve`` and ignored.
        """
        return self.run_serve(
            runs_root=runs_root,
            work_dir=work_dir,
            ro_binds=ro_binds,
            env=env,
            network=network,
            command=command,
            extra_rw_binds=extra_rw_binds,
            extra_ro_dest_binds=extra_ro_dest_binds,
            egress_socket=egress_socket,
        )

    def launch_agent_runtime_bus(
        self,
        *,
        run_id: str,
        bus_socket: str,
        tenant: str,
        extra_rw_binds: "list[tuple[str, str]] | None" = None,
        extra_ro_binds: "list[str | tuple[str, str]] | tuple[str | tuple[str, str], ...]" = (),
        env_overrides: "dict[str, str] | None" = None,
        snapshot: ServeSnapshot | None = None,
    ) -> "BusRunHandle":
        """Launch one Agent Runtime process (the sandboxed ``__projectws_v1_...``

        Project path) — same contract as
        ``RootlessGvisorProvider.launch_agent_runtime_bus``, built on
        ``self.run()``'s bind-mount plumbing instead of an OCI bundle.
        """
        if snapshot is not None:
            # Rootless gVisor already refuses this identically — see
            # checkpoint_serve's docstring; no rootless caller depends on it.
            raise RuntimeError(
                "Agent Runtime snapshot restore requires rootful gVisor; "
                "bubblewrap has no snapshot/restore equivalent"
            )
        env = _workflow_python_env()
        env.update(env_overrides or {})
        env.update({
            "VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX": "1",
            "VIBECANVAS_RUNTIME_TENANT_ID": tenant,
            _BUS_SOCK_ENV: IN_SANDBOX_BUS_SOCK,
        })
        command = [
            sys.executable,
            "-m",
            "vibecanvas_api.services.agent_runtime.sandbox_entry",
        ]

        rw_binds: list[tuple[str, str]] = []
        seen_destinations: set[str] = set()
        for destination, source in extra_rw_binds or []:
            if destination in seen_destinations:
                continue
            if not os.path.exists(source):
                os.makedirs(source, exist_ok=True)
            elif not (os.path.isdir(source) or os.path.isfile(source)):
                raise ValueError("writable bind source must be a file or directory")
            rw_binds.append((destination, source))
            seen_destinations.add(destination)
        bus_host_dir = os.path.dirname(bus_socket)
        os.makedirs(bus_host_dir, exist_ok=True)
        rw_binds.append((IN_SANDBOX_BUS_DIR, bus_host_dir))
        if not rw_binds:
            raise RuntimeError("agent runtime requires at least one writable bind")

        ro_binds: list[str] = list(_workflow_python_binds())
        ro_dest_binds: list[tuple[str, str]] = []
        for binding in extra_ro_binds:
            if isinstance(binding, tuple):
                if binding not in ro_dest_binds:
                    ro_dest_binds.append(binding)
            elif binding not in ro_binds:
                ro_binds.append(binding)

        loop_thread = None
        if config.sandbox_egress_mode == "proxy":
            egress = self._sandbox_egress_setup(run_id, set())
            if egress is None:  # pragma: no cover - guarded by proxy mode above
                raise RuntimeError("Agent Runtime egress broker was not configured")
            loop_thread, egress_socket, proxy_env = egress
            egress_host_dir = os.path.dirname(egress_socket)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env.update(proxy_env)
            env["VC_RUNTIME_EGRESS_PROXY"] = proxy_env["HTTP_PROXY"]

        runtime_network = (
            "host" if config.sandbox_egress_mode == "host-network" else "none"
        )
        env.setdefault("PATH", _DEFAULT_PATH)
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            argv = self._build_argv(
                command=command,
                env=env,
                network=runtime_network,
                rw_binds=rw_binds,
                extra_ro_binds=ro_binds,
                ro_dest_binds=ro_dest_binds,
            )
            proc = subprocess.Popen(
                argv,
                # A resident Runtime can emit more than one pipe buffer of
                # diagnostics over its lifetime; nothing consumes these
                # streams, so PIPE would eventually back-pressure it (mirrors
                # RootlessGvisorProvider.launch_agent_runtime_bus).
                stdout=None,
                stderr=None,
                start_new_session=True,
            )
        except Exception:
            if loop_thread is not None:
                loop_thread.stop()
            raise
        return BusRunHandle(
            proc=proc,
            bundle_dir="",
            state_root="",
            container_id=uuid.uuid4().hex,
            exec_dir=bus_host_dir,
            network=runtime_network,
            egress_loop_thread=loop_thread,
        )

    def launch_workflow_bus(
        self,
        *,
        run_dir: str,
        workflow: dict,
        inputs: dict,
        run_id: str,
        bus_socket: str,
        tenant: "str | None" = None,
        allow_hosts: "set[str] | None" = None,
        kind: str = "workflow",
        lib_overlay: str | None = None,
    ) -> "BusRunHandle":
        """Launch a sandboxed workflow run without blocking — same contract as

        ``RootlessGvisorProvider.launch_workflow_bus``, reusing its
        ``_build_workflow_invocation`` (provider-agnostic: it only writes the
        ``__exec__`` job files and resolves the Python runtime binds/env).
        """
        command, env, binds = self._build_workflow_invocation(
            run_dir=run_dir, workflow=workflow, inputs=inputs,
            run_id=run_id, tenant=tenant, kind=kind,
        )
        env = dict(env)
        rw_binds = [("/run", run_dir)]
        bus_host_dir = os.path.dirname(bus_socket)
        os.makedirs(bus_host_dir, exist_ok=True)
        rw_binds.append((IN_SANDBOX_BUS_DIR, bus_host_dir))
        env[_BUS_SOCK_ENV] = IN_SANDBOX_BUS_SOCK

        egress = self._sandbox_egress_setup(run_id, allow_hosts)
        if egress is not None:
            loop_thread, egress_socket, proxy_env = egress
            env = {**env, **proxy_env}
            egress_host_dir = os.path.dirname(egress_socket)
            os.makedirs(egress_host_dir, exist_ok=True)
            rw_binds.append((IN_SANDBOX_EGRESS_DIR, egress_host_dir))
            env[_EGRESS_SOCK_ENV] = IN_SANDBOX_EGRESS_SOCK
            env[_EGRESS_PORT_ENV] = str(_EGRESS_PROXY_PORT)
            network = "none"
        else:
            loop_thread = None
            network = None

        ro_dest_binds: list[tuple[str, str]] = []
        if lib_overlay is not None:
            ro_dest_binds.append((IN_SANDBOX_LIB_OVERLAY, lib_overlay))
            env[_LIB_OVERLAY_ENV] = IN_SANDBOX_LIB_OVERLAY

        env.setdefault("PATH", _DEFAULT_PATH)
        env.setdefault("PYTHONUNBUFFERED", "1")
        argv = self._build_argv(
            command=command, env=env, network=network, rw_binds=rw_binds,
            extra_ro_binds=binds, ro_dest_binds=ro_dest_binds,
        )
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except Exception:
            if loop_thread is not None:
                loop_thread.stop()
            raise
        exec_dir = os.path.join(run_dir, "__exec__")
        return BusRunHandle(
            proc=proc, bundle_dir="", state_root="",
            container_id=uuid.uuid4().hex, exec_dir=exec_dir,
            egress_loop_thread=loop_thread,
        )

    def stop_run(self, handle: "BusRunHandle", *, kill: bool = False) -> None:
        """Tear down a launch_{agent_runtime,workflow}_bus run. Best-effort +

        idempotent, mirroring ``RootlessGvisorProvider.stop_run`` minus the
        ``runsc kill``/``delete`` calls it has no equivalent of.
        """
        force = bool(kill)
        if not force:
            try:
                handle.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                force = True
            except Exception:
                force = True
        if force:
            try:
                os.killpg(os.getpgid(handle.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                handle.proc.wait(timeout=2.0)
            except Exception:
                pass
        loop_thread = getattr(handle, "egress_loop_thread", None)
        if loop_thread is not None:
            try:
                loop_thread.stop()
            except Exception:
                pass
