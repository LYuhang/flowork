"""A reusable sandbox execution slot with a private /run and RPC controller.

One workflow revision is installed per slot. While an approval is pending the
slot remains occupied; another invocation must use another slot or be refused.
The host owns admission and persistent results. This class owns only OS process,
communication and artifact workspace lifecycle.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import shutil
import sys

from .gvisor import _workflow_python_env
from .warm import WarmGvisorPool
from .workflow_rpc import WorkflowRpcClient
from vibecanvas_api.config import config
from vibecanvas_api.services.deployment_completion import complete_before_cancelling


class WorkflowRpcSlot:
    def __init__(
        self,
        *,
        provider,
        root: str,
        revision: str,
        workflow: dict,
        node_id: str | None = None,
        rw_binds: list[tuple[str, str]] | None = None,
        ro_binds: list[tuple[str, str]] | None = None,
        env: dict | None = None,
        egress_socket: str | None = None,
    ):
        self.provider = provider
        self.root = Path(root)
        self.revision = revision
        self.workflow = workflow
        self.node_id = node_id
        self.rw_binds = rw_binds or []
        self.ro_binds = ro_binds or []
        self.env = env or {}
        self.egress_socket = egress_socket
        self.handle = None
        self.client = None
        self.invocation_id: str | None = None
        self._boot_lock = asyncio.Lock()
        self._drains: list[asyncio.Task] = []
        self._booting = False
        self._startup_output = ""
        self._egress_broker = None
        self._egress_lease = None

    @property
    def alive(self) -> bool:
        return self.handle is not None and self.handle.proc.poll() is None

    def process_identity(self) -> dict:
        from .process_identity import capture_process
        if not self.alive:
            raise RuntimeError("execution_lost")
        return capture_process(self.handle.proc.pid)

    async def start(self) -> None:
        async with self._boot_lock:
            if self.alive:
                return
            if self.invocation_id is not None:
                raise RuntimeError("execution_lost")
            work = self.root / "control"
            artifacts = self.root / "artifacts"
            for directory in (work, artifacts):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            token = secrets.token_urlsafe(48)
            # Keep the UDS pathname short: Linux has a 108-byte sockaddr limit.
            socket_path = work / "rpc.sock"
            if len(os.fsencode(socket_path)) >= 108:
                raise ValueError("workflow RPC control path is too long")
            env = {**_workflow_python_env(), **self.env}
            mounts = [(dest, src) for dest, src in self.rw_binds if dest not in {"/run", "/runs", "/work"}]
            mounts.append(("/run", str(artifacts)))
            self._booting = True
            self._startup_output = ""
            launch = asyncio.create_task(
                asyncio.to_thread(
                    self._launch,
                    artifacts,
                    work,
                    env,
                    mounts,
                    token,
                )
            )
            try:
                self.handle = await asyncio.shield(launch)
            except asyncio.CancelledError:
                # A cancelled to_thread await does not cancel process creation.
                # Recover its handle and kill it before releasing the slot.
                while not launch.done():
                    try:
                        await asyncio.shield(launch)
                    except asyncio.CancelledError:
                        continue
                self.handle = launch.result()
                await self._close()
                raise
            except BaseException:
                await self._close()
                raise
            # Drain controller pipes so incidental library output cannot fill
            # an OS pipe and freeze execution. Business output is sent over RPC;
            # never copy arbitrary node output into host diagnostic logs.
            self._drains = [
                asyncio.create_task(self._discard_output(stream))
                for stream in (self.handle.proc.stdout, self.handle.proc.stderr)
            ]
            self.client = WorkflowRpcClient(str(socket_path), token)
            try:
                async with asyncio.timeout(30):
                    while True:
                        if not self.alive:
                            await asyncio.gather(*self._drains, return_exceptions=True)
                            raise RuntimeError("workflow RPC process exited during startup: " + self._startup_output)
                        try:
                            await self.client.call("hello", timeout=2)
                            break
                        except (FileNotFoundError, ConnectionRefusedError):
                            await asyncio.sleep(0.05)
                    await self.client.call(
                        "install", revision=self.revision, workflow=self.workflow, node_id=self.node_id
                    )
                    self._booting = False
                    self._startup_output = ""
            except BaseException:
                await self._close()
                raise

    def _launch(self, artifacts, work, env, mounts, token):
        # Network broker and child process are one owned launch operation. The
        # async caller shields this thread and recovers its handle on cancellation.
        try:
            if config.sandbox_egress_mode == "proxy" and self.egress_socket is None:
                setup = self.provider._sandbox_egress_setup(str(self.root), set())
                if setup is None:
                    raise RuntimeError("workflow egress broker was not configured")
                self._egress_broker, self.egress_socket, proxy_env = setup
                env.update(proxy_env)
            return self.provider.run_serve(
                runs_root=str(artifacts),
                work_dir=str(work),
                ro_binds=WarmGvisorPool._runtime_ro_binds(),
                env=env,
                command=[sys.executable, "-m", "vibecanvas_engine.runtime.rpc", "/work/rpc.sock", "1"],
                extra_rw_binds=mounts,
                extra_ro_dest_binds=self.ro_binds,
                egress_socket=self.egress_socket,
                network="none" if self.egress_socket else None,
                bootstrap_data={"token": token},
            )
        except BaseException:
            if self._egress_broker is not None:
                self._egress_broker.stop()
                self._egress_broker = None
                self.egress_socket = None
            raise

    async def invoke(self, invocation_id: str, inputs: dict, context: dict, *, require_approval_resume=False) -> dict:
        if not self.alive:
            raise RuntimeError("execution_lost")
        if self.invocation_id not in (None, invocation_id):
            raise RuntimeError("concurrency_limit_exceeded")
        self.invocation_id = invocation_id
        if self._egress_broker is not None and self._egress_lease is None:
            from .egress_policy import compute_allow_hosts

            hosts = compute_allow_hosts(self.workflow, user_id="", creds_mapping=context.get("llm_credentials") or {})
            self._egress_lease = await asyncio.to_thread(self._egress_broker.acquire_allow_hosts, hosts)
        # After a transport exception, retain ownership until reconciled. The
        # remote may already have accepted and started the invocation.
        return await self.client.call(
            "invoke",
            invocation_id=invocation_id,
            revision=self.revision,
            inputs=inputs,
            context={**context, "run_dir": "/run"},
            require_approval_resume=require_approval_resume,
        )

    async def release(self, invocation_id: str, through: int) -> None:
        if self.invocation_id != invocation_id:
            raise RuntimeError("execution owner mismatch")
        state = await self.client.call("status", invocation_id=invocation_id)
        if state["status"] not in {"succeeded", "failed", "timed_out", "cancelled"}:
            raise RuntimeError("cannot release a live execution")
        # Host must persist results and artifacts before calling release.
        await self.client.call("acknowledge", invocation_id=invocation_id, through=through)
        await asyncio.to_thread(self._clear_artifacts)
        if self._egress_broker is not None:
            await asyncio.to_thread(self._egress_broker.release_allow_hosts, self._egress_lease)
            self._egress_lease = None
        self.invocation_id = None

    async def _discard_output(self, stream) -> None:
        # Resident pipes can stay silent for hours. A blocking reader would
        # occupy the shared executor for that entire lifetime, starving DNS,
        # process cleanup and artifact persistence after only a few slots.
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport = None
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, stream)
            while chunk := await reader.read(4096):
                if self._booting:
                    self._startup_output = (self._startup_output + chunk.decode("utf-8", errors="replace"))[-4000:]
        finally:
            if transport is not None:
                transport.close()
            else:
                stream.close()

    def _clear_artifacts(self) -> None:
        # Keep the mounted directory inode. Remove only its entries after the
        # owner has copied durable artifacts and every execution worker exited.
        for item in (self.root / "artifacts").iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()

    async def close(self) -> None:
        async with self._boot_lock:
            await self._close()

    @complete_before_cancelling
    async def _close(self) -> None:
        if self.handle is not None:
            await asyncio.to_thread(self.provider.stop_serve, self.handle)
            if self.handle.proc.poll() is None:
                raise RuntimeError("workflow_process_stop_unconfirmed")
            self.handle = None
        await asyncio.gather(*self._drains, return_exceptions=True)
        self._drains = []
        self.client = None
        if self._egress_broker is not None:
            await asyncio.to_thread(self._egress_broker.stop)
            self._egress_broker = None
            self._egress_lease = None
            self.egress_socket = None
