"""Turn-owned Browser CLI process inside the Agent sandbox.

The Host supplies only scoped connection material through the existing private
Runtime bus. Page code never executes on the Host. Every command is authorized
again. Interruption kills the worker and closes its relay; the extension then
best-effort terminates outstanding evaluations and releases held input. Already
completed effects and page-owned timers cannot be rolled back by killing Node.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import tempfile
import uuid
from collections.abc import Awaitable, Callable

from vibecanvas_api.flowork_cli import browser_cli
from vibecanvas_api.security.redaction import redact_text
from vibecanvas_api.services.agent_runtime.mcp_browser_transport import start_browser_cdp_relay


class BrowserStartupError(RuntimeError):
    """A safe, actionable initialization failure before any page command."""


def _startup_diagnostic(message, material, local_bearer):
    text = str(message)
    for private in (local_bearer, material.get("bearer"), material.get("endpoint")):
        if private:
            text = text.replace(private, "[redacted]")
    return redact_text(text)[:2000]

AUTHORIZATION_INTERVAL_SECONDS = 5


class BrowserCliRuntime:
    def __init__(self, authorize: Callable[[str, dict], Awaitable[dict]], *, commit=None, transfer=None):
        self.authorize = authorize
        self.commit = commit
        self.transfer = transfer
        self._transfer_calls: set[str] = set()
        self._lock = asyncio.Lock()
        self._process = None
        self._relay = None
        self._fence = None
        self._sequence = 0
        self._closed = False
        self._private_root = None
        self._stderr_tail = bytearray()
        self._stderr_task = None

    async def _drain_stderr(self, stream):
        while chunk := await stream.read(4096):
            self._stderr_tail.extend(chunk)
            del self._stderr_tail[:-8192]

    async def _start(self, material):
        local_bearer = secrets.token_urlsafe(32)
        try:
            await self._start_runtime(material, local_bearer)
        except BrowserStartupError:
            raise
        except Exception as error:
            diagnostic = _startup_diagnostic(f"{type(error).__name__}: {error}", material, local_bearer)
            logging.getLogger(__name__).warning("Browser CLI startup failed: %s", diagnostic)
            raise BrowserStartupError(f"Browser CLI could not start: {diagnostic}") from error

    async def _start_runtime(self, material, local_bearer):
        if self._closed:
            raise RuntimeError("Browser CLI turn has ended")
        executable = shutil.which(os.environ.get("BROWSER_CLI_COMMAND", "flowork-browser-runtime"))
        if not executable:
            raise BrowserStartupError("Browser CLI runtime is not installed in the sandbox. Contact the platform operator.")
        self._private_root = tempfile.mkdtemp(prefix="flowork-browser-private-")
        self._relay = await start_browser_cdp_relay(local_bearer=local_bearer)
        await self._relay.activate(upstream_url=material["endpoint"], upstream_bearer=material["bearer"])
        self._process = await asyncio.create_subprocess_exec(
            executable, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
            # JSON frame sizes are not file-size limits. File transfer itself is
            # chunked by the browser runtime rather than written on this pipe.
            limit=2**31 - 1,
        )
        self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))
        self._sequence += 1
        self._process.stdin.write(json.dumps({
            "id": self._sequence, "operation": "initialize",
            "arguments": {"endpoint": self._relay.endpoint, "bearer": local_bearer, "private_root": self._private_root},
        }).encode() + b"\n")
        await self._process.stdin.drain()
        line = await asyncio.wait_for(self._process.stdout.readline(), timeout=30)
        if not line:
            # A missing binary/module or permission failure must not masquerade
            # as a browser disconnect. Drain only a bounded diagnostic tail.
            if self._stderr_task.done():
                await self._stderr_task
            else:
                await asyncio.wait({self._stderr_task}, timeout=1)
            diagnostic = _startup_diagnostic(self._stderr_tail.decode(errors="replace"), material, local_bearer)
            raise BrowserStartupError(f"Browser runtime exited before initialization (exit {self._process.returncode}): {diagnostic or 'No stderr diagnostic.'}")
        response = json.loads(line)
        if response.get("id") != self._sequence:
            raise BrowserStartupError("Browser initialization returned an invalid response correlation.")
        if response.get("result", {}).get("status") != "succeeded":
            diagnostic = _startup_diagnostic(response.get("result", {}).get("message") or "No connection diagnostic was returned.", material, local_bearer)
            logging.getLogger(__name__).warning("Browser CLI initialization failed: %s", diagnostic)
            raise BrowserStartupError(f"Browser CLI could not connect: {diagnostic}")
        self._fence = material["fence"]

    async def execute(self, operation, arguments, emit):
        arguments = browser_cli.validate(operation, arguments)
        async with self._lock:
            if self._closed:
                return {"status": "failed", "error": "runtime_unavailable", "message": "The originating Agent turn has ended."}
            dispatched = False
            try:
                material = await self.authorize(operation, arguments)
                if material.get("error"):
                    # Revocation applies to an already-connected worker too;
                    # retaining that connection would leave stale authority.
                    await self._stop()
                    return {**material, "status": "failed"}
                if self._process is not None and (self._process.returncode is not None or self._fence != material["fence"]):
                    await self._stop()
                if self._process is None:
                    await self._start(material)
                self._sequence += 1
                sequence = self._sequence
                frame = json.dumps({"id": sequence, "operation": operation,
                                    "arguments": arguments}, allow_nan=False).encode() + b"\n"
                dispatched = True
                self._process.stdin.write(frame)
                await self._process.stdin.drain()
                pending = asyncio.create_task(self._process.stdout.readline())
                loop = asyncio.get_running_loop()
                renew_at = loop.time() + AUTHORIZATION_INTERVAL_SECONDS
                try:
                    while True:
                        if loop.time() >= renew_at:
                            # Live permission/lease changes stop even a long JS
                            # script, including one continuously printing progress.
                            # This is renewal, not a business timeout.
                            renewed = await self.authorize(operation, arguments, expected_fence=self._fence)
                            if renewed.get("error") or renewed.get("fence") != self._fence:
                                raise PermissionError("Browser permission or lease changed during execution")
                            await self._renew_transfers()
                            renew_at = loop.time() + AUTHORIZATION_INTERVAL_SECONDS
                            await emit({"_transport": "heartbeat"})
                        done, _ = await asyncio.wait({pending}, timeout=max(0, renew_at - loop.time()))
                        if pending not in done:
                            continue
                        line = pending.result()
                        if not line:
                            raise ConnectionError("Browser runtime exited before returning a result")
                        response = json.loads(line)
                        if response.get("id") != sequence:
                            raise ValueError("Browser runtime correlation mismatch")
                        if "progress" in response:
                            await emit({"_progress": response["progress"]})
                            pending = asyncio.create_task(self._process.stdout.readline())
                            continue
                        if "transfer" in response:
                            control = response["transfer"]
                            decision = await self._approve_transfer(control.get("input"), emit)
                            self._process.stdin.write(json.dumps({"transfer_reply": control.get("request_id"),
                                                                 "result": decision}).encode() + b"\n")
                            await self._process.stdin.drain()
                            pending = asyncio.create_task(self._process.stdout.readline())
                            continue
                        result = response.get("result")
                        if not isinstance(result, dict):
                            raise ValueError("Invalid browser runtime response")
                        return await self._commit_artifacts(operation, arguments, result, emit)
                finally:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            except asyncio.CancelledError:
                await self._stop()
                raise
            except BrowserStartupError as error:
                await self._stop()
                return {"status": "failed", "error": "browser_initialization_failed", "message": str(error),
                        "hint": "No page action was dispatched. Check the extension connection or report this startup diagnostic before retrying."}
            except Exception:
                await self._stop()
                unknown = dispatched and operation in browser_cli.WRITE_OPERATIONS
                return {"status": "unknown" if unknown else "failed",
                        "error": "result_unknown" if unknown else "browser_unavailable",
                        "message": "The browser connection or runtime was interrupted.",
                        "hint": "Inspect the page and output files before retrying; an action may already have happened." if unknown else "Check the extension connection, then retry the observation."}
            finally:
                await self._finish_transfers()

    async def _renew_transfers(self, *, exclude=None):
        for call_id in tuple(self._transfer_calls):
            if call_id == exclude:
                continue
            decision = await self.transfer({"action": "renew", "call_id": call_id})
            if decision.get("status") != "approved":
                raise PermissionError("File-transfer permission is no longer active")

    async def _approve_transfer(self, input, emit):
        if self.transfer is None:
            return {"status": "denied", "message": "File-transfer approval is unavailable. No bytes were read."}
        call_id = uuid.uuid4().hex
        self._transfer_calls.add(call_id)
        response = await self.transfer({"action": "start", "call_id": call_id, "input": input})
        if response.get("pending"):
            await emit({"_progress": {"status": response.get("status"),
                                     "message": response.get("message", "Waiting for file-transfer approval.")}})
        while response.get("pending"):
            await asyncio.sleep(1)
            await emit({"_transport": "heartbeat"})
            # The stdout loop is paused here. A later human decision must not
            # silently expire an earlier approved transfer in the same script.
            # The pending call renews itself through poll; renew only the rest.
            await self._renew_transfers(exclude=call_id)
            response = await self.transfer({"action": "poll", "call_id": call_id})
        await emit({"_progress": {"status": response.get("status"), "message": response.get("message", "File-transfer approval ended.")}})
        if response.get("status") != "approved":
            # A script may catch a denied transfer and continue ordinary browser
            # work. Do not let a later lease renewal abort that unrelated work.
            await self.transfer({"action": "finish", "call_id": call_id})
            self._transfer_calls.discard(call_id)
        return response

    async def _finish_transfers(self):
        calls, self._transfer_calls = self._transfer_calls, set()
        if not self.transfer or not calls:
            return
        async def finish():
            await asyncio.gather(*(self.transfer({"action": "finish", "call_id": id}) for id in calls), return_exceptions=True)
        cleanup = asyncio.create_task(finish())
        try:
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=5)
        except (Exception, asyncio.CancelledError):
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)

    async def _commit_artifacts(self, operation, arguments, result, emit):
        artifacts = result.pop("_artifacts", [])
        if not artifacts:
            return result
        acknowledgement = {}
        pending = None
        try:
            if self.commit is None:
                raise RuntimeError("Browser artifact persistence is unavailable")
            pending = asyncio.create_task(self.commit(operation, arguments, artifacts))
            while not pending.done():
                await asyncio.wait({pending}, timeout=5)
                if not pending.done():
                    await emit({"_transport": "heartbeat"})
            acknowledgement = pending.result()
            if acknowledgement.get("error"):
                raise RuntimeError("Browser artifact persistence was not acknowledged")
            receipts = acknowledgement.get("artifacts")
            expected = {(item["file"], item["sha256"], item["bytes"]) for item in artifacts}
            if (not isinstance(receipts, list)
                    or {(item["file"], item["sha256"], item["bytes"]) for item in receipts} != expected
                    or any(item.get("persistence") not in {"durable", "sandbox"} for item in receipts)):
                raise RuntimeError("Browser artifact receipt is incomplete")
        except asyncio.CancelledError:
            raise
        except Exception:
            return {**result, "status": "failed", "error": "artifact_persistence_failed",
                    "message": "Browser output was saved locally, but durable persistence was not fully confirmed.",
                    "hint": "Do not repeat the browser action. Inspect the saved files and report or retry storage persistence separately.",
                    "artifacts": acknowledgement.get("artifacts", []), "local_artifacts": artifacts}
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        committed = acknowledgement.get("artifacts", [])
        result["artifacts"] = committed
        # Keep nested command output consistent with the final storage receipt.
        by_path = {item["file"]: item for item in committed}
        for item in (result.get("result"), (result.get("result") or {}).get("artifact") if isinstance(result.get("result"), dict) else None):
            if isinstance(item, dict) and isinstance(item.get("file"), str) and item["file"] in by_path:
                item.update(by_path[item["file"]])
                if item.get("persistence") == "durable":
                    item["message"] = "The exact output bytes were saved to durable Chat storage."
                else:
                    item["message"] = "The output is saved in the sandbox only; this path is not durable Chat storage."
        return result

    async def _stop(self):
        process, self._process = self._process, None
        self._fence = None
        if process is not None:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            # A user script may have created descendants; do not let them retain
            # this turn's private pipes or continue after its parent exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stderr_task, self._stderr_task = self._stderr_task, None
        if stderr_task is not None:
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        self._stderr_tail.clear()
        relay, self._relay = self._relay, None
        try:
            if relay is not None:
                await relay.close()
        finally:
            private_root, self._private_root = self._private_root, None
            if private_root is not None:
                # Only this turn's exact mkdtemp result, never an Agent-supplied
                # destination or a shared workspace. Also runs after SIGKILL.
                try:
                    if os.path.islink(private_root):
                        os.unlink(private_root)
                    else:
                        await asyncio.to_thread(shutil.rmtree, private_root)
                except FileNotFoundError:
                    pass

    async def close(self):
        self._closed = True
        await self._stop()
