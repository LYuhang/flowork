"""Runtime-local CLI transport with turn-scoped dispatch; credentials stay on Host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import shlex
import sys
import tempfile
from uuid import uuid4
from collections.abc import Awaitable, Callable

from vibecanvas_api.flowork_cli import cli


def platform_guidance() -> str:
    return Path(cli.__file__).with_name("AGENTS.md").read_text(encoding="utf-8")


def prepare_platform_guidance(runtime_root: str) -> None:
    """Project only the platform-owned Codex-home guidance, never workspace files."""
    path = Path(runtime_root) / "AGENTS.md"
    content = platform_guidance()
    if path.is_symlink():
        raise RuntimeError("platform guidance path must not be a symlink")
    if path.exists():
        previous = path.read_text(encoding="utf-8")
        if previous == content:
            return
        if "Managed by Flowork, not a memory file." not in previous:
            raise RuntimeError("refusing to replace unmanaged Codex-home AGENTS.md")
    fd, staged = tempfile.mkstemp(prefix=".flowork-guidance-", dir=runtime_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), 0o444)
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def guidance_revision() -> str:
    return hashlib.sha256(platform_guidance().encode()).hexdigest()


def _guidance_versions(runtime_root: str) -> dict:
    try:
        value = json.loads((Path(runtime_root) / ".flowork-cli-guidance.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def needs_guidance_update(runtime_root: str, thread_id: str | None) -> bool:
    return bool(thread_id and _guidance_versions(runtime_root).get(thread_id) != guidance_revision())


def mark_guidance_loaded(runtime_root: str, thread_id: str) -> None:
    versions = _guidance_versions(runtime_root)
    versions.pop(thread_id, None)
    versions[thread_id] = guidance_revision()
    versions = dict(list(versions.items())[-64:])
    fd, staged = tempfile.mkstemp(prefix=".flowork-cli-", dir=runtime_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(versions, stream)
        os.replace(staged, Path(runtime_root) / ".flowork-cli-guidance.json")
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


class CliGateway:
    """Stable launcher for resident threads; each accepted call pins its turn.

    Socket access is limited by the existing sandbox boundary, not a claim that
    same-UID processes are mutually isolated. Only explicit allowlisted calls
    is forwarded; the private Runtime bus supplies the execution capability.
    """

    def __init__(self) -> None:
        self._invoke: Callable[[str, dict], Awaitable[dict]] | None = None
        self._server: asyncio.Server | None = None
        self._directory: str | None = None
        self._tasks: set[asyncio.Task] = set()
        self._env: dict[str, str] = {}
        self._document_complete = None
        self._browser = None

    async def activate(self, invoke: Callable[[str, dict], Awaitable[dict]], *, document_complete=None, browser_authorize=None, browser_commit=None, browser_transfer=None) -> dict[str, str]:
        if self._invoke is not None:
            raise RuntimeError("CLI gateway already has an active turn")
        self._document_complete = document_complete
        if browser_authorize is not None:
            from .browser_cli_runtime import BrowserCliRuntime
            self._browser = BrowserCliRuntime(browser_authorize, commit=browser_commit, transfer=browser_transfer)
        if self._server is not None:
            self._invoke = invoke
            return dict(self._env)
        directory = tempfile.mkdtemp(prefix="fw-cli-")
        self._directory = directory
        endpoint = str(Path(directory) / "bridge.sock")
        try:
            # A standalone stdlib program avoids importing the full API in every
            # shell call. Its endpoint belongs to this sandbox Runtime only.
            source = Path(cli.__file__).read_text(encoding="utf-8")
            task_source = Path(cli.__file__).with_name("task_cli.py").read_text(encoding="utf-8")
            task_module = Path(directory) / "task_cli.py"
            task_module.write_text(task_source, encoding="utf-8")
            task_module.chmod(0o400)
            deployment_module = Path(directory) / "deployment_cli.py"
            deployment_module.write_text(Path(cli.__file__).with_name("deployment_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
            deployment_module.chmod(0o400)
            knowledge_module = Path(directory) / "knowledge_cli.py"
            knowledge_module.write_text(Path(cli.__file__).with_name("knowledge_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
            knowledge_module.chmod(0o400)
            document_module = Path(directory) / "document_cli.py"
            document_module.write_text(Path(cli.__file__).with_name("document_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
            document_module.chmod(0o400)
            diagram_module = Path(directory) / "diagram_cli.py"
            diagram_module.write_text(Path(cli.__file__).with_name("diagram_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
            diagram_module.chmod(0o400)
            browser_module = Path(directory) / "browser_cli.py"
            browser_module.write_text(Path(cli.__file__).with_name("browser_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
            browser_module.chmod(0o400)
            skill_module = Path(directory) / "skill_cli.py"
            skill_module.write_text(Path(cli.__file__).with_name("skill_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
            skill_module.chmod(0o400)
            resource_module = Path(directory) / "resource_cli.py"
            resource_module.write_text(Path(cli.__file__).with_name("resource_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
            resource_module.chmod(0o400)
            source = source.replace("sys.exit(main())", f"sys.exit(main(socket_path={endpoint!r}))")
            launcher = Path(directory) / "flowork-cli"
            launcher.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
            launcher.chmod(0o500)
            # Non-interactive login Bash may reset PATH in /etc/profile. Bash
            # reads BASH_ENV afterwards, including in nested `bash -lc` calls.
            shell_environment = Path(directory) / "shell-env.sh"
            previous = os.environ.get("BASH_ENV")
            shell_environment.write_text(
                (f"if [ -f {shlex.quote(previous)} ]; then . {shlex.quote(previous)}; fi\n" if previous else "")
                + f'export PATH={shlex.quote(directory)}:"$PATH"\n', encoding="utf-8",
            )
            shell_environment.chmod(0o400)
            self._server = await asyncio.start_unix_server(self._handle, path=endpoint)
            os.chmod(endpoint, 0o600)
            self._env = {
                "PATH": directory + os.pathsep + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "FLOWORK_CLI_SOCKET": endpoint,
                "FLOWORK_CLI_BIN": str(launcher),
                "BASH_ENV": str(shell_environment),
            }
            self._invoke = invoke
            return dict(self._env)
        except BaseException:
            await self.close()
            raise

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> bytes:
        # Chunk size controls buffering/backpressure, not total message size.
        # readline/readuntil would reject JSON above StreamReader's line limit.
        body = bytearray()
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                raise ValueError("incomplete CLI request")
            end = chunk.find(b"\n")
            if end >= 0:
                body.extend(chunk[:end])
                return bytes(body)
            body.extend(chunk)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        # Capture before any await: an in-flight request must never inherit a
        # later turn's identity. Idle runtimes cannot dispatch CLI operations.
        invoke = self._invoke
        document_complete = self._document_complete
        browser = self._browser
        operation = ""
        dispatched = False
        try:
            line = await asyncio.wait_for(self._read_request(reader), timeout=5)
            body = json.loads(line)
            if (
                not isinstance(body, dict)
                or set(body) != {"operation", "arguments"}
                or not isinstance(body.get("operation"), str)
                or body["operation"] not in cli.OPERATIONS
                or not isinstance(body.get("arguments"), dict)
            ):
                raise ValueError("unsupported CLI request")
            operation = body["operation"]
            arguments = cli.validate_arguments(operation, body["arguments"])
            if invoke is None:
                result = cli.error("runtime_unavailable", "No Agent turn is active.", "Run this command during an active Agent turn.")
            else:
                dispatched = True
                if operation in cli.browser_cli.OPERATIONS:
                    if browser is None:
                        result = {"status": "failed", **cli.error("browser_runtime_unavailable", "Browser CLI is unavailable in this turn.", "Use the extension side-panel Browser Chat.")}
                    else:
                        result = await self._stream_browser(browser, operation, arguments, reader, writer)
                elif operation in cli.document_cli.OPERATIONS | cli.diagram_cli.OPERATIONS:
                    result = await self._stream_document(operation, arguments, reader, writer, document_complete)
                elif operation in cli.RUN_OPERATIONS:
                    result = await self._stream_run(invoke, operation, arguments, reader, writer)
                else:
                    result = await self._stream_call(invoke, operation, arguments, reader, writer)
        except (ValueError, UnicodeError):
            result = cli.uncertain_result() if dispatched and operation in cli.WRITE_OPERATIONS else cli.error("invalid_arguments", "Unsupported or invalid CLI request.", "Run flowork-cli workflow --help.")
        except TimeoutError:
            result = cli.uncertain_result() if dispatched and operation in cli.WRITE_OPERATIONS else cli.error("request_timeout", "The platform request timed out.", "Retry this read-only query.")
        except asyncio.CancelledError:
            result = cli.uncertain_result() if dispatched and operation in cli.WRITE_OPERATIONS else cli.error("runtime_unavailable", "The originating Agent turn has ended.", "Use a new active Agent turn.")
        except Exception:
            result = cli.uncertain_result() if dispatched and operation in cli.WRITE_OPERATIONS else cli.error("platform_unavailable", "The platform request failed.", "Retry or contact platform support.")
        try:
            encoded = json.dumps(result, ensure_ascii=False).encode() + b"\n"
            for offset in range(0, len(encoded), 64 * 1024):
                writer.write(encoded[offset:offset + 64 * 1024])
                await asyncio.wait_for(writer.drain(), timeout=5)
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except (OSError, TimeoutError):
                pass
            finally:
                self._tasks.discard(task)

    @staticmethod
    async def _send_run_event(writer, event):
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        for offset in range(0, len(encoded), 64 * 1024):
            writer.write(encoded[offset:offset + 64 * 1024])
            await asyncio.wait_for(writer.drain(), timeout=5)

    async def _stream_browser(self, browser, operation, arguments, reader, writer):
        disconnected = asyncio.create_task(reader.read(1))
        execution = asyncio.create_task(browser.execute(
            operation, arguments, lambda event: self._send_run_event(writer, event),
        ))
        try:
            done, _ = await asyncio.wait({execution, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise ConnectionError("Browser CLI disconnected")
            return execution.result()
        finally:
            for task in (execution, disconnected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(execution, disconnected, return_exceptions=True)

    async def _stream_document(self, operation, arguments, reader, writer, completed):
        """Execute beside the sandbox files, with a private trusted result pipe."""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "vibecanvas_api.document_runtime.worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            limit=sys.maxsize,
        )
        disconnected = asyncio.create_task(reader.read(1))
        pending = None
        try:
            process.stdin.write(json.dumps({"operation": operation, "arguments": arguments}).encode())
            await process.stdin.drain()
            process.stdin.close()
            while True:
                pending = asyncio.create_task(process.stdout.readline())
                while not pending.done():
                    done, _ = await asyncio.wait({pending, disconnected}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                    if disconnected in done:
                        raise ConnectionError("CLI disconnected")
                    if not done:
                        await self._send_run_event(writer, {"_transport": "heartbeat"})
                line = pending.result()
                if not line:
                    raise RuntimeError("File CLI worker exited without a result")
                result = json.loads(line)
                if "_progress" in result:
                    await self._send_run_event(writer, result)
                    continue
                await process.wait()
                if process.returncode:
                    raise RuntimeError("File CLI worker failed")
                if completed is not None:
                    completed(operation, arguments, result)
                result.pop("_image_hashes", None)
                return result
        finally:
            disconnected.cancel()
            if pending is not None:
                pending.cancel()
            await asyncio.gather(disconnected, *([pending] if pending is not None else []), return_exceptions=True)
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), timeout=2)
                except (ProcessLookupError, TimeoutError):
                    pass
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()

            else:
                # A renderer may exit leaving helpers behind. This private
                # group belongs to this CLI invocation, even after its leader exits.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def _stream_call(self, invoke, operation, arguments, reader, writer):
        call_id = uuid4().hex
        disconnected = asyncio.create_task(reader.read(1))

        async def control(name, body):
            task = asyncio.create_task(invoke(name, {"call_id": call_id, **body}))
            try:
                done, _ = await asyncio.wait({task, disconnected}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    raise ConnectionError("CLI disconnected")
                if task not in done:
                    raise TimeoutError("CLI control channel did not respond")
                return task.result()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            started = await control("cli.start", {"operation": operation, "arguments": arguments})
            if "error" in started:
                return started
            ack = 0
            last_heartbeat = asyncio.get_running_loop().time()
            while True:
                reply = await control("cli.poll", {"ack": ack})
                if "error" in reply:
                    return reply
                event = reply.get("event")
                if event is not None:
                    if event.get("terminal"):
                        return event["result"]
                    if "progress" in event:
                        await self._send_run_event(writer, {"_progress": event["progress"]})
                    ack = reply["sequence"]
                if asyncio.get_running_loop().time() - last_heartbeat >= 5:
                    await self._send_run_event(writer, {"_transport": "heartbeat"})
                    last_heartbeat = asyncio.get_running_loop().time()
                done, _ = await asyncio.wait({disconnected}, timeout=0.25)
                if done:
                    raise ConnectionError("CLI disconnected")
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            try:
                await asyncio.wait_for(invoke("cli.cancel", {"call_id": call_id}), timeout=6)
            except (Exception, asyncio.CancelledError):
                pass  # Host turn-finally and lease expiry fence lost callers.

    async def _stream_run(self, invoke, operation, arguments, reader, writer):
        # The signed identity is captured by invoke for this original turn.
        # EOF (including SIGKILL) must cancel even while waiting for Host.
        disconnected = asyncio.create_task(reader.read(1))
        run_id = arguments["run_id"]

        async def call(name, values):
            task = asyncio.create_task(invoke(name, values))
            try:
                done, _ = await asyncio.wait({task, disconnected}, timeout=15,
                                             return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    raise ConnectionError("CLI disconnected")
                if task not in done:
                    raise TimeoutError("Execution control timed out")
                return task.result()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            started = await call(operation, arguments)
            if "error" in started:
                return started
            await self._send_run_event(writer, started)
            ack = 0
            unavailable_polls = 0
            last_output = asyncio.get_running_loop().time()
            while True:
                reply = await call("workflow.run.poll", {"run_id": run_id, "ack": ack})
                if reply.get("error") == "authorization_unavailable":
                    # This is a read of the same execution/cursor, never a
                    # retry of run/start. Every attempt still reauthorizes.
                    # Do not discard an already delivered result because the
                    # authorization datastore briefly failed during final drain.
                    unavailable_polls += 1
                    if unavailable_polls <= 2:
                        await self._send_run_event(writer, {
                            "status": "running", "run_id": run_id,
                            "message": "Authorization is temporarily unavailable; retrying the same result query.",
                        })
                        done, _ = await asyncio.wait({disconnected}, timeout=0.25 * unavailable_polls)
                        if done:
                            raise ConnectionError("CLI disconnected")
                        continue
                    return {**reply, "hint": "The original execution was already accepted. "
                            "Keep partial output and inspect its result/status before retrying; do not automatically rerun the workflow."}
                if "error" in reply:
                    return reply
                unavailable_polls = 0
                event = reply["event"]
                if event is not None:
                    if event.get("terminal"):
                        return event
                    await self._send_run_event(writer, event)
                    ack = reply["sequence"]
                    last_output = asyncio.get_running_loop().time()
                    continue
                if asyncio.get_running_loop().time() - last_output >= 5:
                    await self._send_run_event(writer, {"status": "running", "run_id": run_id, "message": "Waiting for execution progress."})
                    last_output = asyncio.get_running_loop().time()
                # Short polling, not one long Host operation: approvals and
                # cancellation share this bus and must remain responsive.
                done, _ = await asyncio.wait({disconnected}, timeout=0.5)
                if done:
                    raise ConnectionError("CLI disconnected")
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            try:
                await asyncio.wait_for(invoke("workflow.run.cancel", {"run_id": run_id}), timeout=6)
            except (Exception, asyncio.CancelledError):
                # Host turn-finally + polling lease cover Runtime/bus loss.
                pass

    async def deactivate(self) -> None:
        self._invoke = None
        self._document_complete = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        browser, self._browser = self._browser, None
        if browser is not None:
            await browser.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.deactivate()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
        self._env = {}
