"""Shared workflow execution helpers for resident sandbox sessions.

This module is the single host-side protocol for running workflow jobs inside a
``SandboxSession``. Jobs share the same staging protocol and resolve declared
dependencies through one content-addressed, idempotent preparation path.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import AsyncIterator

import structlog

from vibecanvas_api.config import config
from vibecanvas_api.services.llm_credentials_inject import (
    inject_into_run_context_async,
)
from vibecanvas_api.services.env.overlay_builder import ensure_overlay
from vibecanvas_api.services.env.overlay_key import compute_overlay_key
from vibecanvas_api.services.vfs_run_context import clear_run_contents

logger = structlog.get_logger(__name__)
_RUN_JOB_LOCKS: dict[str, asyncio.Lock] = {}


class WorkflowSandboxRunError(RuntimeError):
    """A workflow sandbox job failed before producing a result."""


@dataclass(frozen=True)
class WorkflowRunLocation:
    run_dir: str
    runs_root: str
    run_subpath: str
    workflow_run_id: str


@dataclass(frozen=True)
class WorkflowJobResult:
    result_json: dict
    submit_status: dict
    location: WorkflowRunLocation


def _code_requirements(workflow: dict | None) -> str:
    if not isinstance(workflow, dict):
        return ""
    meta = workflow.get("__meta__")
    settings = meta.get("settings") if isinstance(meta, dict) else None
    value = settings.get("code_requirements") if isinstance(settings, dict) else None
    return value.strip() if isinstance(value, str) else ""


async def prepare_code_pythonpath(
    workflow: dict | None,
    *,
    session=None,
) -> str | None:
    """Prepare one Workflow execution environment's incremental packages.

    Interactive node/workflow execution passes its resident Workflow sandbox
    session.  The selected dependency layer is then initialized once per
    requirements revision and retained on that session; later runs against the
    same warm sandbox do not call the overlay builder again.

    Plain Chat never calls this helper while its workspace/runtime sandbox is
    starting. Explorer, Preview, edit, save and generic sandbox prewarm also do
    not install Workflow dependencies. Interactive execution caches the chosen
    layer on its session; Deployment and Task paths call the uncached wrapper
    below, which still resolves through the same content-addressed builder.
    """
    requirements = _code_requirements(workflow)
    if not requirements:
        return None

    dependency_key = compute_overlay_key(requirements)

    async def _build() -> str | None:
        built = await _ensure_dependency_overlay(requirements, session=session)
        if _overlay_value(built, "status") != "ready":
            detail = _overlay_value(built, "error_log") or (
                f"overlay status is {_overlay_value(built, 'status')!r}"
            )
            raise WorkflowSandboxRunError(
                f"Python dependency preparation failed for {requirements!r}: {detail}"
            )
        # A requirements file containing comments/whitespace only legitimately
        # has no incremental overlay. The platform base remains available.
        return _overlay_value(built, "path")

    if session is None:
        return await _build()

    if getattr(session, "_workflow_dependency_key", None) == dependency_key:
        return getattr(session, "_workflow_dependency_pythonpath", None)

    lock = getattr(session, "_workflow_dependency_lock", None)
    if lock is None:
        # Lightweight test/protocol doubles do not need to implement private
        # SandboxSession details. Attach the same lifecycle state lazily.
        lock = asyncio.Lock()
        setattr(session, "_workflow_dependency_lock", lock)
    async with lock:
        if getattr(session, "_workflow_dependency_key", None) == dependency_key:
            return getattr(session, "_workflow_dependency_pythonpath", None)
        path = await _build()
        setattr(session, "_workflow_dependency_key", dependency_key)
        setattr(session, "_workflow_dependency_pythonpath", path)
        return path


def _overlay_value(result, key: str):
    return result.get(key) if isinstance(result, dict) else getattr(result, key, None)


async def _ensure_dependency_overlay(requirements: str, *, session=None):
    """Build on sandboxd when remote; keep embedded/test execution local."""
    if session is not None and getattr(session, "remote", False):
        manager = getattr(session, "_manager", None)
        if manager is None:
            raise WorkflowSandboxRunError(
                "remote sandbox session has no dependency manager"
            )
        return await manager.ensure_workflow_dependencies(requirements)
    if session is None and config.sandbox_service_mode == "service":
        from vibecanvas_api.services.sandbox.manager import get_sandbox_manager

        return await get_sandbox_manager().ensure_workflow_dependencies(
            requirements,
        )
    return await ensure_overlay(requirements)


async def ensure_code_pythonpath(
    workflow: dict | None,
    *,
    session=None,
) -> str | None:
    """Resolve or prepare the dependency layer for an executing Workflow.

    The overlay builder is content-addressed and protected by a per-key file
    lock, so this is a cache lookup on warm paths and a single shared build on a
    cold path. Non-interactive execution must be self-contained: a Deployment,
    Batch or Scheduled Task cannot depend on somebody first opening the
    Workflow editor after a service or cache restart.
    """
    requirements = _code_requirements(workflow)
    if not requirements:
        return None
    prepared = await _ensure_dependency_overlay(requirements, session=session)
    if _overlay_value(prepared, "status") != "ready":
        detail = _overlay_value(prepared, "error_log") or (
            f"overlay status is {_overlay_value(prepared, 'status')!r}"
        )
        raise WorkflowSandboxRunError(
            f"Python dependency preparation failed for {requirements!r}: {detail}"
        )
    return _overlay_value(prepared, "path")


def _merge_stage_extra(run_dir: str, *, code_pythonpath: str | None) -> None:
    """Merge the prepared package path without clobbering credential extras."""
    exec_dir = os.path.join(run_dir, "__exec__")
    os.makedirs(exec_dir, exist_ok=True)
    path = os.path.join(exec_dir, "extra.json")
    current: dict = {}
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict):
            current = value
    except (OSError, ValueError):
        pass
    if code_pythonpath:
        current["code_pythonpath"] = code_pythonpath
    else:
        current.pop("code_pythonpath", None)
    if current:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, default=str)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def workflow_run_location(session, *, workflow_run_id: str | None = None) -> WorkflowRunLocation:
    run_id = workflow_run_id or getattr(session, "workflow_run_id", None)
    if not run_id:
        raise WorkflowSandboxRunError("workflow sandbox has no run id")
    run_dir = getattr(session, "workflow_run_dir", None) or getattr(session, "run_dir", None)
    if not run_dir and getattr(session, "remote", False):
        # A remote proxy intentionally has no daemon host path. This value is a
        # local lock/debug identity only; sandboxd derives the real mount from
        # tenant + logical run_subpath and ignores caller path data.
        run_dir = f"sandbox://{run_id}"
        return WorkflowRunLocation(
            run_dir=run_dir,
            runs_root="",
            run_subpath=run_id,
            workflow_run_id=run_id,
        )
    if not run_dir:
        raise WorkflowSandboxRunError("workflow sandbox has no /run directory")
    return WorkflowRunLocation(
        run_dir=run_dir,
        runs_root=os.path.dirname(run_dir),
        run_subpath=os.path.basename(run_dir),
        workflow_run_id=run_id,
    )


def _run_job_lock(run_dir: str) -> asyncio.Lock:
    """Serialize jobs that share one fixed ``run_dir/__exec__`` channel."""
    lock = _RUN_JOB_LOCKS.get(run_dir)
    if lock is None:
        lock = asyncio.Lock()
        _RUN_JOB_LOCKS[run_dir] = lock
    return lock


def stage_workflow_job(
    runs_root: str,
    run_subpath: str,
    workflow: dict,
    inputs: dict,
    extra: dict | None = None,
) -> None:
    exec_dir = os.path.join(runs_root, run_subpath, "__exec__")
    os.makedirs(exec_dir, exist_ok=True)
    with open(os.path.join(exec_dir, "workflow.json"), "w", encoding="utf-8") as f:
        json.dump(workflow, f, ensure_ascii=False)
    with open(os.path.join(exec_dir, "inputs.json"), "w", encoding="utf-8") as f:
        json.dump(inputs, f, ensure_ascii=False)
    extra_path = os.path.join(exec_dir, "extra.json")
    if extra:
        with open(extra_path, "w", encoding="utf-8") as f:
            json.dump(extra, f, ensure_ascii=False, default=str)
    else:
        try:
            os.remove(extra_path)
        except FileNotFoundError:
            pass
    with open(os.path.join(exec_dir, "job.json"), "w", encoding="utf-8") as f:
        json.dump({"kind": "workflow"}, f, ensure_ascii=False)


def stage_node_job(run_dir: str, node: dict, inputs: dict, extra: dict | None = None) -> None:
    exec_dir = os.path.join(run_dir, "__exec__")
    os.makedirs(exec_dir, exist_ok=True)
    with open(os.path.join(exec_dir, "job.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"kind": "node", "node": node, "inputs": inputs, "extra": extra or {}},
            f,
            ensure_ascii=False,
            default=str,
        )


def read_result_json(runs_root: str, run_subpath: str) -> dict | None:
    try:
        with open(
            os.path.join(runs_root, run_subpath, "__exec__", "result.json"),
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)
    except Exception:
        return None


async def submit_workflow_job(
    session,
    *,
    tenant: str,
    run_id: str,
    run_subpath: str,
    timeout: float = 600.0,
) -> dict:
    return await session.submit_sandbox_job(
        {
            "kind": "workflow",
            "tenant": tenant or "",
            "run_id": run_id,
            "run_subpath": run_subpath,
        },
        timeout=timeout,
    )


async def run_workflow_once(
    session,
    *,
    tenant_id: str,
    workflow: dict,
    inputs: dict,
    workflow_run_id: str | None = None,
    user_id: str,
    workflow_id: str,
    execution_id: str,
    execution_resource_type: str,
    execution_principal_type: str = "user",
    execution_principal_id: str | None = None,
    execution_principal_generation: int = 0,
    clear_run: bool = True,
    timeout: float = 600.0,
    install_dependencies: bool = False,
) -> WorkflowJobResult:
    loc = workflow_run_location(session, workflow_run_id=workflow_run_id)
    injection_claims = {
        "user_id": user_id,
        "workflow_id": workflow_id,
        "execution_id": execution_id,
        "execution_resource_type": execution_resource_type,
    }
    if execution_principal_type != "user":
        injection_claims.update({
            "principal_type": execution_principal_type,
            "principal_id": execution_principal_id,
            "principal_generation": execution_principal_generation,
        })
    run_context = await inject_into_run_context_async(
        {}, workflow, tenant_id, **injection_claims,
    )
    runtime_extra = (
        {"llm_credentials": run_context["llm_credentials"]}
        if run_context.get("llm_credentials") else None
    )
    stop = asyncio.Event()
    async for msg in stream_workflow_job(
        stop=stop,
        workflow=workflow,
        inputs=inputs,
        workflow_run_id=loc.workflow_run_id,
        tenant_id=tenant_id,
        session=session,
        timeout=timeout,
        install_dependencies=install_dependencies,
        runtime_extra=runtime_extra,
        clear_run=clear_run,
    ):
        mtype = msg.get("type")
        if mtype == "result":
            result = {
                "final_outputs": msg.get("final_outputs") or {},
                "error_dict": msg.get("error_dict") or {},
                "execution_time": msg.get("execution_time"),
            }
            return WorkflowJobResult(
                result_json=result,
                submit_status={"status": "success"},
                location=loc,
            )
        if mtype == "timeout":
            raise WorkflowSandboxRunError(msg.get("message") or "workflow job timed out")
    raise WorkflowSandboxRunError("workflow job produced no result")


async def run_node_once(
    session,
    *,
    tenant_id: str,
    node: dict,
    inputs: dict,
    workflow_run_id: str | None = None,
    extra: dict | None = None,
    workflow: dict | None = None,
    clear_run: bool = False,
    timeout: float = 600.0,
    install_dependencies: bool = False,
) -> WorkflowJobResult:
    loc = workflow_run_location(session, workflow_run_id=workflow_run_id)
    # Dependency builds are content-addressed and may take longer than a job.
    # They do not touch the shared __exec__ channel, so keep them outside the
    # per-run staging lock.
    code_pythonpath = (
        await prepare_code_pythonpath(workflow, session=session)
        if install_dependencies
        else await ensure_code_pythonpath(workflow, session=session)
    )
    if clear_run:
        clear_owned_run = getattr(session, "clear_workflow_run", None)
        if clear_owned_run is not None:
            await clear_owned_run()
        else:
            await clear_run_contents(loc.workflow_run_id, tenant_id)
    submit_node = getattr(session, "submit_node_job", None)
    if submit_node is not None:
        node_extra = dict(extra or {})
        if code_pythonpath:
            node_extra["code_pythonpath"] = code_pythonpath
        else:
            node_extra.pop("code_pythonpath", None)
        response = await submit_node(
            node=node, inputs=inputs, extra=node_extra,
            tenant=tenant_id or "", run_id=loc.workflow_run_id,
            run_subpath=loc.run_subpath,
            timeout=timeout,
        )
        status = response.get("status") or {}
        result = response.get("result")
    else:
        async with _run_job_lock(loc.run_dir):
            node_extra = dict(extra or {})
            if code_pythonpath:
                node_extra["code_pythonpath"] = code_pythonpath
            else:
                node_extra.pop("code_pythonpath", None)
            await asyncio.to_thread(
                stage_node_job, loc.run_dir, node, inputs, node_extra,
            )
            status = await session.submit_sandbox_job(
                {
                    "kind": "node",
                    "tenant": tenant_id or "",
                    "run_id": loc.workflow_run_id,
                    "run_subpath": loc.run_subpath,
                },
                timeout=timeout,
            )
            result = read_result_json(loc.runs_root, loc.run_subpath)
    if result is None:
        raise WorkflowSandboxRunError(
            status.get("error_message")
            or status.get("error")
            or "node produced no result"
        )
    await session.writeback_vfs()
    return WorkflowJobResult(result_json=result, submit_status=status, location=loc)


def run_workflow_once_sync(*args, **kwargs) -> WorkflowJobResult:
    return asyncio.run(run_workflow_once(*args, **kwargs))


async def _next_sandbox_message_or_stop(stream, stop: asyncio.Event) -> dict | None:
    recv_task = asyncio.create_task(stream.__anext__())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {recv_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            recv_task.cancel()
            with suppress(BaseException):
                await recv_task
            return None
        stop_task.cancel()
        with suppress(BaseException):
            await stop_task
        return recv_task.result()
    except StopAsyncIteration:
        return None
    finally:
        for task in (recv_task, stop_task):
            if not task.done():
                task.cancel()
                with suppress(BaseException):
                    await task


async def stream_workflow_job(
    *,
    stop: asyncio.Event,
    workflow: dict,
    inputs: dict,
    workflow_run_id: str,
    tenant_id: str,
    session,
    exec_id: str | None = None,
    timeout: float | None = 120.0,
    install_dependencies: bool = False,
    runtime_extra: dict | None = None,
    allow_hosts: set[str] | list[str] | tuple[str, ...] | None = None,
    clear_run: bool = False,
) -> AsyncIterator[dict]:
    loc = workflow_run_location(session, workflow_run_id=workflow_run_id)
    started = time.perf_counter()
    writeback_attempted = False
    code_pythonpath = (
        await prepare_code_pythonpath(workflow, session=session)
        if install_dependencies
        else await ensure_code_pythonpath(workflow, session=session)
    )
    if allow_hosts is None:
        from vibecanvas_api.services.sandbox.egress_policy import (
            compute_allow_hosts,
        )
        allow_hosts = compute_allow_hosts(
            workflow,
            user_id="",
            creds_mapping=(runtime_extra or {}).get("llm_credentials") or {},
        )
    async with _run_job_lock(loc.run_dir):
        # Full workflow executions own the run channel. Clear stale outputs and,
        # critically, a prior __exec__/cancel marker while holding the same lock
        # used for staging/submission so another execution cannot race the reset.
        # Node-only execution intentionally keeps its existing cached inputs.
        if clear_run:
            clear_owned_run = getattr(session, "clear_workflow_run", None)
            if clear_owned_run is not None:
                await clear_owned_run(loc.workflow_run_id)
            else:
                await clear_run_contents(loc.workflow_run_id, tenant_id)
        stream = session.submit_workflow_stream(
            tenant=tenant_id,
            workflow=workflow,
            inputs=inputs,
            run_id=loc.workflow_run_id,
            run_subpath=loc.run_subpath,
            run_dir=loc.run_dir,
            extra=runtime_extra,
            code_pythonpath=code_pythonpath,
            allow_hosts=allow_hosts,
            timeout=timeout,
        )
        logger.warning(
            "workflow_execution_stage",
            stage="sandbox_pool_ready",
            wf_id=loc.workflow_run_id,
            exec_id=exec_id,
            workers=max(1, int(config.sandbox_fileop_workers)),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        try:
            async with aclosing(stream):
                while True:
                    msg = await _next_sandbox_message_or_stop(stream, stop)
                    if msg is None:
                        if stop.is_set():
                            await session.cancel_workflow_run(
                                tenant=tenant_id,
                                run_id=loc.workflow_run_id,
                                run_subpath=loc.run_subpath,
                            )
                        return
                    # A result/timeout is terminal at the workflow protocol
                    # boundary.  Finish the owned VFS writeback before making
                    # that frame observable: callers intentionally return or
                    # break on terminal frames, and async-for does not promise
                    # to close a nested async generator at that point.
                    if msg.get("type") in {"result", "timeout"}:
                        writeback_attempted = True
                        await session.writeback_vfs()
                    yield msg
        finally:
            if not writeback_attempted:
                await session.writeback_vfs()
