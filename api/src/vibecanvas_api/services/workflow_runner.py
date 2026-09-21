"""Synchronous entry point for running a workflow from Postgres state.

``run_workflow_sandboxed_sync`` is the live sync runner, called from the
background worker ``batch_exec`` worker body (a synchronous context: no running event
loop) and the deployment-invoke path. Workflow loading goes through the
``SyncWorkflowRepo`` facade, which opens its own short NullPool async
session per call. The workflow is executed inside a gVisor OS sandbox, which is
the sole execution path; there is no in-process
fallback) and the legacy ``(previous_outputs, error_dict, execution_time)``
tuple still falls out byte-identically.

RLS contract: ``current_sync_tenant_id`` must be set
BEFORE any sync repo call so the short-lived session emits
``SET LOCAL app.tenant_id``. This module is the sole place the background worker
worker sets it; the agent's own entry point sets it elsewhere.

``drain_astream`` and ``load_workflow_version`` support
for the async (in-API-process) sync-invoke endpoint. These run INSIDE the
FastAPI event loop so they must be async and never call ``asyncio.run``. They
reuse the same engine event shape as ``Workflow._trigger_inner`` so the
(outputs, errors, time) tuple stays canonical across both code paths.
"""
from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import structlog
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.authorization.types import ResourceType
from vibecanvas_api.config import config
from vibecanvas_api.observability.workflow import instrumented_drain
from vibecanvas_api.services.run_workspace import RunWorkspace
from vibecanvas_api.services.llm_credentials_inject import (
    inject_into_run_context_sync,
)
from vibecanvas_api.services.env.overlay_builder import ensure_overlay
from vibecanvas_api.services.sandbox import (
    EngineNeedsHostNode,
    SandboxUnavailable,
    get_sandbox_provider,
)
from vibecanvas_api.services.sandbox.admission import sync_sandbox_admission
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.sandbox.egress_policy import compute_allow_hosts
from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.storage.sync_repo import SyncWorkflowRepo
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.security.content_encryption import content_encryption_service
from vibecanvas_engine.workflow import Workflow

logger = structlog.get_logger(__name__)


def run_workflow_sandboxed_sync(
    *,
    workflow_id: str,
    inputs: dict,
    tenant_id: str,
    user_id: str,
    run_id: str | None = None,
    workflow_dict: dict | None = None,
    execution_resource_type: str = ResourceType.DEPLOYMENT_INVOCATION.value,
    execution_principal_type: str = "user",
    execution_principal_id: str | None = None,
    execution_principal_generation: int = 0,
) -> tuple[dict, dict, float]:
    """Run the workflow once INSIDE a gVisor OS-sandbox (the sole sync runner).

    Returns the canonical ``(previous_outputs, error_dict, execution_time)``
    tuple (seconds) consumed by the background worker batch worker and deployment-invoke
    paths.

    ``workflow_dict``: when provided, run THAT content directly (skipping the
    ``SyncWorkflowRepo.get_current_workflow`` load) so a deployment's pinned
    (possibly non-head) version is honored. When ``None`` (batch +
    REST), load the CURRENT/head version as before.

    There is no in-process fallback: the gVisor
    sandbox is the sole execution path for the sync (batch/deploy) runner. The
    three previously-tolerated fall-throughs now raise CLEAR errors instead of
    silently running in-process:

    * **No sandbox available** (``SandboxUnavailable`` — e.g. a box with no
      ``runsc``): re-raised with a clear message — workflow execution requires
      the gVisor sandbox.
    * **In-memory object store** (``run_dir is None`` — the process-local dict
      can't be bind-mounted into the sandbox): raises ``RuntimeError``.
    * **Host-only node** (``EngineNeedsHostNode`` — the provider's pure-engine
      guard rejects a non-pure node): re-raised with a clear message. Note that
      every REGISTERED node type is in ``SANDBOX_RUNNABLE_NODE_TYPES``
      (``ENGINE_PURE_NODE_TYPES`` ∪ ``KnowledgeSearchNode``/``SubAgentNode``), so
      this is a defensive guard, not a routine path.

    This synchronous entry is called from sync contexts (a background worker
    thread / ``asyncio.to_thread``), so the BLOCKING ``provider.run_workflow``
    is legal (no running loop to deadlock on). We do NOT add any ``asyncio.run``
    on a running loop here.

    The ``provider.run_workflow`` call is mode-AGNOSTIC: a future
    snapshot/warm provider implementing the same ``run_workflow`` contract drops
    in unchanged (no one-shot-only assumptions encoded here). ``RunWorkspace``
    owns the execution-local run directory and cleanup.
    """
    # Set the tenant ContextVar before any sync repository call so the
    # short-lived session emits ``SET LOCAL app.tenant_id``.
    current_sync_tenant_id.set(tenant_id)
    workflow_dict = (
        workflow_dict
        if workflow_dict is not None
        else SyncWorkflowRepo(username=user_id).get_current_workflow(
            workflow_id)
    )
    run_id = run_id or uuid4().hex

    # Every executing surface is self-contained. The content-addressed overlay
    # builder is a fast lookup on warm paths and performs one lock-protected
    # build on cold paths, so Deployment/Task execution never relies on a user
    # first opening the Workflow editor after a restart or cache replacement.
    settings = (workflow_dict.get("__meta__") or {}).get("settings") or {}
    requirements = settings.get("code_requirements")
    if config.sandbox_service_mode == "service":
        injection_claims = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "workflow_id": workflow_id,
            "execution_id": run_id,
            "execution_resource_type": execution_resource_type,
        }
        if execution_principal_type != "user":
            injection_claims.update({
                "principal_type": execution_principal_type,
                "principal_id": execution_principal_id,
                "principal_generation": execution_principal_generation,
            })
        run_context = inject_into_run_context_sync(
            {"run_id": run_id, "run_dir": None},
            workflow_dict,
            **injection_claims,
        )
        try:
            allow_hosts = compute_allow_hosts(
                workflow_dict,
                user_id=user_id,
                creds_mapping=run_context.get("llm_credentials") or {},
            )
        except Exception:  # pragma: no cover - fail closed in proxy mode
            logger.warning("egress_allowlist_compute_failed", exc_info=True)
            allow_hosts = set()
        response = asyncio.run(get_sandbox_manager().run_workflow_once(
            workflow_id=workflow_id,
            workflow=workflow_dict,
            inputs=inputs,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            extra=(
                {"llm_credentials": run_context["llm_credentials"]}
                if run_context.get("llm_credentials") else None
            ),
            allow_hosts=sorted(allow_hosts),
            requirements=(
                requirements.strip()
                if isinstance(requirements, str) and requirements.strip()
                else None
            ),
        ))
        return (
            response.get("final_outputs") or {},
            response.get("error_dict") or {},
            float(response.get("execution_time") or 0.0),
        )
    lib_overlay: str | None = None
    if isinstance(requirements, str) and requirements.strip():
        prepared = asyncio.run(ensure_overlay(requirements.strip()))
        if prepared.status != "ready":
            detail = prepared.error_log or f"overlay status is {prepared.status!r}"
            raise RuntimeError(
                "workflow dependency preparation failed "
                f"({requirements.strip()!r}: {detail})"
            )
        lib_overlay = prepared.path

    try:
        provider = get_sandbox_provider()
    except SandboxUnavailable as exc:
        # SANDBOX-ONLY: no OS-sandbox here (runsc missing) → hard error, NOT an
        # in-process fallback. The gVisor sandbox is the sole execution path.
        raise SandboxUnavailable(
            "workflow execution requires the gVisor sandbox (runsc); there is "
            "no in-process fallback"
        ) from exc

    # ``keep_run=True`` preserves the existing result-viewer metadata contract.
    with RunWorkspace(
        run_id,
        tenant_id,
        wf_id=workflow_id,
        user_id=user_id,
        keep_run=True,
    ) as ws:
        run_dir = ws.run_context["run_dir"]
        # Resolve the PromptNode saved-credential mapping ONCE against THIS
        # workspace's run_context — it carries ``llm_credentials`` for the
        # host-side ``__exec__/extra.json`` serialization (the in-sandbox engine
        # is DB-free and can't look creds up itself). Only written when the
        # workflow references a saved-model PromptNode (legacy runs
        # byte-identical).
        injection_claims = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "workflow_id": workflow_id,
            "execution_id": run_id,
            "execution_resource_type": execution_resource_type,
        }
        if execution_principal_type != "user":
            injection_claims.update({
                "principal_type": execution_principal_type,
                "principal_id": execution_principal_id,
                "principal_generation": execution_principal_generation,
            })
        run_context = inject_into_run_context_sync(
            ws.run_context, workflow_dict, **injection_claims,
        )
        creds = run_context.get("llm_credentials")

        # The sandbox bind-mounts a REAL host run dir (``run_dir`` ↔ ``/run``);
        # there is nothing to mount when the configured object store can't
        # materialize one (``InMemoryObjectStore`` — its dict is process-local,
        # so ``materialize_prefix`` raises and ``build_run_context`` yields
        # ``run_dir=None``). Production stores (filesystem / S3) always give a
        # real dir. SANDBOX-ONLY: an in-memory deploy fundamentally can't be
        # sandboxed, so this is a hard configuration error, NOT an in-process
        # fallback.
        if run_dir is None:
            raise RuntimeError(
                "workflow execution requires a real object store "
                "(filesystem/S3); the in-memory store cannot be bind-mounted "
                "into the sandbox"
            )

        if creds:
            exec_dir = os.path.join(run_dir, "__exec__")
            os.makedirs(exec_dir, exist_ok=True)
            with open(os.path.join(exec_dir, "extra.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"llm_credentials": creds}, f, ensure_ascii=False)

        # Plan B B6: per-run egress allowlist (auto-derived LLM + MCP hosts ∪
        # user-declared). Only consumed in ``proxy`` mode (ignored in the default
        # host-network mode), so it is always safe to compute + pass. Fail-soft:
        # a computation error degrades to the derived-LLM hosts (see
        # ``compute_allow_hosts``) rather than crashing the run or silently
        # opening everything; an outright raise here is caught and we pass an
        # empty set (proxy then blocks all egress — fail-closed, never open).
        try:
            allow_hosts = compute_allow_hosts(
                workflow_dict,
                user_id=user_id,
                creds_mapping=creds or {},
            )
        except Exception:  # pragma: no cover — defensive; wrapper is fail-soft
            logger.warning("egress_allowlist_compute_failed", exc_info=True)
            allow_hosts = set()

        try:
            with sync_sandbox_admission(tenant_id=tenant_id):
                result = provider.run_workflow(
                    run_dir=run_dir,
                    workflow=workflow_dict,
                    inputs=inputs,
                    run_id=run_id,
                    tenant=tenant_id,
                    allow_hosts=allow_hosts,
                    lib_overlay=lib_overlay,
                    mount_dir=ws.mount_dir,
                )
        except EngineNeedsHostNode as exc:
            # SANDBOX-ONLY: a non-pure (host-only) node — re-raised with a clear
            # message, NOT an in-process fallback. Every REGISTERED node type is
            # in SANDBOX_RUNNABLE_NODE_TYPES, so this is a defensive guard.
            raise EngineNeedsHostNode(
                f"node type not supported in the sandbox: {exc}"
            ) from exc

        return (result.final_outputs, result.error_dict, result.execution_time)


async def drain_astream(
    wf: Workflow, inputs: dict, run_context: dict | None = None,
) -> tuple[dict, dict, float]:
    """Consume ``Workflow.astream()`` to completion in the current event loop.

    Returns ``(previous_outputs, error_dict, exec_time_ms)`` — same
    semantic shape as :py:meth:`Workflow._trigger_inner`, but
    *milliseconds* (not seconds) because the deployment-invoke
    response payload uses milliseconds throughout.

    Event keying matches the engine (`workflow.py:_execute`):
    * ``status == "finished"`` carries ``final_outputs`` + ``error_dict``
      bundled on the same event.
    * ``status == "error"`` is an engine-level critical error — not
      associated with a node id; we file it under ``__engine__``.

    Used by the in-API-process invoke handler so the run stays in the
    same event loop (no ``asyncio.run`` hop, cancellable by request
    disconnect via the engine's ``stop_event``).

    Delegates to the shared instrumented consumer
    (:func:`vibecanvas_api.observability.workflow.instrumented_drain`) so the
    async path gets the same spans/metrics as the sync background worker path. The error
    merge there reproduces this function's historical keying exactly.
    ``instrumented_drain`` returns the engine ``execution_time`` in *seconds*;
    this wrapper converts to *milliseconds* to preserve the response contract
    (``exec_time_ms``) its callers depend on.
    """
    previous_outputs, error_dict, exec_time_s = await instrumented_drain(
        wf, inputs, run_context=run_context)
    elapsed_ms = exec_time_s * 1000.0
    return previous_outputs, error_dict, elapsed_ms


async def load_workflow_version(dep: dict) -> dict:
    """Resolve a deployment's ``version_pin`` to a workflow content dict.

    Admin-role read (``session_scope_admin``) — the row was already
    located by ``resolve_deployment_and_bind_tenant`` under admin scope
    and we just need its pinned encrypted workflow payload.
    Using the admin engine is the conservative path: forward-compat
    with multi-tenant deployments referencing shared workflow templates,
    and uniform with the resolver itself (single role for the whole
    external-entry path before the tenant CV is read by downstream
    code).

    Spec §6.4. Raises HTTPException(500) if the pinned version was
    deleted out from under the deployment (a tenant operator removed
    the row directly; the T5 wf-delete guard prevents this for the
    full-workflow case, but defence-in-depth catches the rare manual
    edit).

    Workflow content is envelope-encrypted. Missing ciphertext is a storage
    integrity failure; the execution path never falls back to a legacy
    plaintext column.
    """
    if dep["version_pin"] == "head":
        sql = (
            "SELECT tenant_id, major, sub, workflow_ciphertext, "
            "workflow_nonce, workflow_key_id FROM workflow_versions WHERE wf_id = :w "
            "ORDER BY major DESC, sub DESC LIMIT 1"
        )
        params = {"w": dep["wf_id"]}
    else:  # 'specific'
        sql = (
            "SELECT tenant_id, major, sub, workflow_ciphertext, "
            "workflow_nonce, workflow_key_id FROM workflow_versions WHERE wf_id = :w "
            "AND major = :m AND sub = :s"
        )
        params = {
            "w": dep["wf_id"],
            "m": dep["pinned_major"],
            "s": dep["pinned_sub"],
        }
    async with session_scope_admin() as s:
        row = (await s.execute(text(sql), params)).one_or_none()
        if row is None:
            raise HTTPException(500, "workflow version not found")
        if not row.workflow_key_id or not row.workflow_ciphertext or not row.workflow_nonce:
            raise HTTPException(500, "workflow version ciphertext is missing")
        await s.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(row.tenant_id)},
        )
        workflow = await content_encryption_service().decrypt_json(
            s,
            key_id=row.workflow_key_id,
            tenant_id=row.tenant_id,
            resource_type="workflow",
            resource_id=dep["wf_id"],
            purpose="workflow_version",
            record_id=f"v{row.major}.sv{row.sub}",
            ciphertext=row.workflow_ciphertext,
            nonce=row.workflow_nonce,
        )
        if not isinstance(workflow, dict):
            raise HTTPException(500, "workflow version is invalid")
        return workflow
