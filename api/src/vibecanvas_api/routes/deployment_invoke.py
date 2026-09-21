"""External deployment endpoints — runs the deployed workflow via api_key/slug/webhook.

T6 ships POST ``/api/v1/deployments/{slug}/invoke`` — the true sync path
that runs the workflow IN the API process via ``Workflow.astream``
without a background queue hop. POST ``/api/v1/deployments/{slug}/runs``
— async submit; enqueues a durable ``deployment_invoke`` workflow and returns
``task_id`` as an opaque deployment invocation id.
T8 will add ``/webhook`` (HMAC verified).

Spec §6 invariant: every external endpoint MUST call
``resolve_deployment_and_bind_tenant`` FIRST. RLS is otherwise unset
and any tenant-scoped query that follows would be silently filtered to
zero rows (FORCE RLS is universal across the business tables).

Auth model:
* ``api`` deployments authenticate by ``Authorization: Bearer <api_key>``
  (the one-shot plaintext returned by T4's create / T5's rotate-key).
* The path slug must match the deployment the key belongs to — a key
  for deployment A cannot invoke deployment B even if the path slug is
  valid for B.

Error mapping intentionally does not reveal resource existence:
* Missing / malformed ``Authorization`` header → 401 (RFC compliance).
* Unknown api_key, disabled deployment, wrong trigger_type, mismatched
  slug → 404 ``"deployment not found"`` (uniform, never leaks which
  axis failed).
* Engine-level / per-node errors → 502 with the per-node ``errors``
  bundle so the caller can debug their workflow without the API
  pretending the run succeeded.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from time import perf_counter
from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import text

from vibecanvas_api.authorization.types import ResourceType
from vibecanvas_api.services.deployment_secret_config import (
    resolve_deployment_hmac_secret,
)
from vibecanvas_api.services.deployments_service import (
    DeploymentsService,
    resolve_deployment_and_bind_tenant,
)
from vibecanvas_api.services.rate_limit import (
    bump_redis_invoke_counter,
    check_rate_limit,
)
from vibecanvas_api.services.tenant_db import tenant_id_var
from vibecanvas_api.services.workflow_runner import (
    load_workflow_version,
    run_workflow_sandboxed_sync,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/deployments", tags=["deployments-invoke"],
)

_WEBHOOK_MAX_BODY_BYTES = 1_048_576
_WEBHOOK_REPLAY_RETENTION_SECONDS = 600


async def _read_body_with_hard_limit(request: Request, *, limit: int) -> bytes:
    """Read at most ``limit`` actual bytes, independent of client headers."""
    stream = getattr(request, "stream", None)
    if callable(stream):
        chunks: list[bytes] = []
        size = 0
        async for chunk in stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="payload too large (max 1MB)",
                )
            chunks.append(chunk)
        return b"".join(chunks)
    # Narrow compatibility seam for direct route unit tests. The post-read
    # length check remains authoritative; a lying Content-Length cannot bypass it.
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="payload too large (max 1MB)",
        )
    return body


async def _claim_webhook_receipt(
    session,
    *,
    tenant_id: uuid.UUID,
    deployment_id: uuid.UUID,
    signature: str,
) -> tuple[uuid.UUID, bool]:
    """Atomically claim an authenticated signature or return its prior run."""
    digest = hashlib.sha256(signature.encode("ascii")).hexdigest()
    invocation_id = uuid.uuid4()
    # Keep the table bounded without making correctness depend on a beat job.
    await session.execute(
        text(
            "DELETE FROM deployment_webhook_receipts "
            "WHERE deployment_id = :deployment_id AND expires_at <= now()"
        ),
        {"deployment_id": deployment_id},
    )
    claimed = (
        await session.execute(
            text(
                """
                INSERT INTO deployment_webhook_receipts (
                    tenant_id, deployment_id, signature_digest,
                    invocation_id, expires_at
                )
                VALUES (
                    :tenant_id, :deployment_id, :signature_digest,
                    :invocation_id,
                    now() + make_interval(secs => :retention_seconds)
                )
                ON CONFLICT (tenant_id, deployment_id, signature_digest)
                DO NOTHING
                RETURNING invocation_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "deployment_id": deployment_id,
                "signature_digest": digest,
                "invocation_id": invocation_id,
                "retention_seconds": _WEBHOOK_REPLAY_RETENTION_SECONDS,
            },
        )
    ).scalar_one_or_none()
    if claimed is not None:
        return claimed, True
    existing = (
        await session.execute(
            text(
                """
                SELECT invocation_id
                FROM deployment_webhook_receipts
                WHERE tenant_id = :tenant_id
                  AND deployment_id = :deployment_id
                  AND signature_digest = :signature_digest
                """
            ),
            {
                "tenant_id": tenant_id,
                "deployment_id": deployment_id,
                "signature_digest": digest,
            },
        )
    ).scalar_one()
    return existing, False


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """Return the bearer token from an ``Authorization`` header, or
    ``None`` if the header is absent / not a ``Bearer`` scheme. Strict
    on the ``Bearer `` prefix — empty token after the space is also
    treated as malformed (caller maps to 401)."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    return token or None


@router.post("/{slug}/invoke")
async def invoke_sync(
    slug: str,
    body: dict,
    authorization: Optional[str] = Header(default=None),
):
    """Spec §6.1 — true sync invoke. Runs the workflow IN the API process
    via :py:meth:`Workflow.astream`. There is no background queue hop or
    intermediate ``tasks`` row — the caller blocks until completion.

    Returns ``{"outputs": ..., "exec_time_ms": ...}`` on success.
    Returns 502 ``{"errors": ..., "exec_time_ms": ...}`` when the
    workflow finished but at least one node produced an error.

    Why every "not authorized" case is 404 (not 403 / 401):
    api_key, slug, enabled, and trigger_type are all *existence* axes
    of the same row — leaking which one failed would let an attacker
    probe slugs by trying random keys against them. The single 404
    response collapses them all.
    """
    api_key = _extract_bearer(authorization)
    if api_key is None:
        # 401 — bearer required. (Malformed/empty bearer is "did not
        # authenticate", not "deployment not found".)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )

    dep = await resolve_deployment_and_bind_tenant(api_key=api_key)
    # All four conditions collapse to 404 — never leak which axis
    # failed, without revealing whether the deployment exists.
    if (
        dep is None
        or not dep["enabled"]
        or dep["trigger_type"] != "api"
        or dep["slug"] != slug
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )

    await check_rate_limit(dep)

    tenant_id = str(tenant_id_var.get())

    async with session_scope(tenant_id=tenant_id) as session:
        service_account_id = dep.get("service_account_id")
        lease = None
        if service_account_id is not None:
            try:
                lease = await ServiceAccountsRepo(session).require_active_lease(
                    service_account_id=uuid.UUID(str(service_account_id)),
                    owner_resource_type="deployment",
                    owner_resource_id=str(dep["id"]),
                )
            except (LookupError, ValueError) as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="deployment execution identity unavailable",
                ) from exc
        invocation_id = await DeploymentInvocationsRepo(session).create(
            tenant_id=uuid.UUID(tenant_id),
            deployment_id=dep["id"],
            wf_id=dep["wf_id"],
            trigger_type=dep["trigger_type"],
            source="sync_api",
            status="running",
        )

    started = perf_counter()
    outputs: dict = {}
    errors: dict = {}
    fatal_http_exc: HTTPException | None = None
    try:
        workflow_dict = await load_workflow_version(dep)
        execution_identity = (
            {
                "execution_principal_type": "service_account",
                "execution_principal_id": str(lease.service_account_id),
                "execution_principal_generation": lease.generation,
            }
            if lease is not None else {}
        )
        outputs, errors, exec_secs = await asyncio.to_thread(
            run_workflow_sandboxed_sync,
            workflow_id=dep["wf_id"],
            inputs=body,
            tenant_id=tenant_id,
            user_id=str(lease.created_by if lease is not None else dep["user_id"]),
            run_id=str(invocation_id),
            workflow_dict=workflow_dict,
            execution_resource_type=ResourceType.DEPLOYMENT_INVOCATION.value,
            **execution_identity,
        )
        exec_time_ms = exec_secs * 1000.0
    except HTTPException as exc:
        exec_time_ms = (perf_counter() - started) * 1000.0
        errors = {"__top__": str(exc.detail)}
        fatal_http_exc = exc
    except Exception as exc:
        exec_time_ms = (perf_counter() - started) * 1000.0
        errors = {"__top__": f"{type(exc).__name__}: {exc}"}

    async with session_scope(tenant_id=tenant_id) as session:
        await DeploymentInvocationsRepo(session).mark_terminal(
            invocation_id,
            status="failed" if errors else "succeeded",
            latency_ms=exec_time_ms,
            error="execution_failed" if errors else None,
            result_summary={
                "output_count": len(outputs) if isinstance(outputs, dict) else 0,
                "error_count": len(errors) if isinstance(errors, dict) else 0,
            },
        )
    if fatal_http_exc is not None:
        raise fatal_http_exc

    # Bump the in-Redis invoke counter (best-effort); a background
    # flusher (``deployments.flush_invoke_counters``) periodically writes
    # batched counters to ``deployments.invoke_count`` + ``last_invoked_at``
    # so this hot path stays write-free. AFTER the run so a crash
    # doesn't double-count.
    await bump_redis_invoke_counter(dep["id"])

    if errors:
        # Workflow ran to completion but produced per-node errors.
        # 502 because the underlying execution (a "remote" workflow run)
        # failed — same shape as a downstream gateway error. We use
        # an explicit Response so the body still serializes (raising
        # HTTPException would lose the ``exec_time_ms`` field).
        return Response(
            status_code=status.HTTP_502_BAD_GATEWAY,
            media_type="application/json",
            content=json.dumps(
                {"errors": errors, "exec_time_ms": exec_time_ms}
            ),
        )
    return {"outputs": outputs, "exec_time_ms": exec_time_ms}


@router.post("/{slug}/runs", status_code=status.HTTP_202_ACCEPTED)
async def invoke_async(
    slug: str,
    body: dict,
    authorization: Optional[str] = Header(default=None),
):
    """Spec §6.2 — async submit. Enqueues a durable ``deployment_invoke``
    workflow and returns its opaque invocation id immediately.

    Auth model is identical to ``invoke_sync`` — Bearer plaintext
    api_key, all "not authorized" cases collapse to 404 to avoid
    leaking which axis (api_key / enabled / trigger_type / slug)
    failed without revealing whether the deployment exists.

    The session remains tenant-bound so future Deployment-specific invocation
    logs can be written without changing the route boundary.
    """
    api_key = _extract_bearer(authorization)
    if api_key is None:
        # 401 — bearer required (RFC compliance, mirrors invoke_sync).
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )

    dep = await resolve_deployment_and_bind_tenant(api_key=api_key)
    if (
        dep is None
        or not dep["enabled"]
        or dep["trigger_type"] != "api"
        or dep["slug"] != slug
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )

    await check_rate_limit(dep)

    tenant_id = tenant_id_var.get()
    async with session_scope(tenant_id=str(tenant_id)) as session:
        svc = DeploymentsService(session, DeploymentsRepo(session))
        task_id = await svc.submit(
            deployment=dep,
            payload=body,
            source="async_api",
        )
    await bump_redis_invoke_counter(dep["id"])
    return {"task_id": str(task_id)}


@router.post("/{slug}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def webhook(slug: str, request: Request):
    """Spec §6.3 — webhook receiver with HMAC verification + size guard.

    No Bearer auth: trust comes from a valid signature over
    ``timestamp + "." + raw_body`` using the deployment's ``hmac_secret``.
    Returns 202 + ``task_id`` once the durable workflow is enqueued.

    Order of checks (rejects cheapest first):

    1. ``Content-Type`` must be ``application/json`` → 415 otherwise.
       (We refuse to even sniff non-JSON.)
    2. ``Content-Length`` header is required and capped at 1 MiB → 413
       otherwise. This is only a cheap pre-filter; the actual streamed bytes
       are independently capped, so a false header cannot bypass the limit.
    3. The actual streamed bytes are capped before any database/KMS lookup.
    4. ``resolve_deployment_and_bind_tenant`` admin-lookup by globally unique
       active slug → 404 on miss / disabled / non-webhook trigger_type.
    5. ``X-Vibecanvas-Timestamp`` window check (±300s) → 401 otherwise.
       This bounds clock skew and receipt retention; it is not by itself a
       replay defense.
    6. HMAC-SHA256 of ``timestamp + "." + raw_body`` with the row's
       ``hmac_secret`` must match ``X-Vibecanvas-Signature`` (``sha256=``
       prefix). Compared with ``hmac.compare_digest`` (constant time).
       Mismatch → 401.
    7. JSON parse of body → 400 on malformed JSON. (Done AFTER HMAC
       so an attacker can't probe parse errors to differentiate
       routes — though the size/timestamp/sig layers already
       neutralise that.)
    8. A durable signature receipt atomically maps the authenticated request
       to one invocation id. Replays within the timestamp window return the
       original id and never enqueue a second workflow.
    """
    import hmac
    import json as _json
    import time as _time

    ct = request.headers.get("Content-Type", "").split(";")[0].strip()
    if ct != "application/json":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="only application/json accepted",
        )

    cl_raw = request.headers.get("Content-Length")
    try:
        cl = int(cl_raw) if cl_raw is not None else None
    except ValueError:
        cl = None
    if cl is None or cl < 0 or cl > _WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="payload too large (max 1MB)",
        )

    body = await _read_body_with_hard_limit(
        request,
        limit=_WEBHOOK_MAX_BODY_BYTES,
    )

    dep = await resolve_deployment_and_bind_tenant(slug=slug)
    if dep is None or not dep["enabled"] or dep["trigger_type"] != "webhook":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="deployment not found",
        )

    sig = request.headers.get("X-Vibecanvas-Signature", "")
    ts = request.headers.get("X-Vibecanvas-Timestamp", "")
    try:
        if abs(_time.time() - int(ts)) > 300:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="timestamp expired",
            )
    except (ValueError, TypeError):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="invalid timestamp",
        )

    tenant_id = str(tenant_id_var.get())
    async with session_scope(tenant_id=tenant_id) as session:
        hmac_secret = await resolve_deployment_hmac_secret(session, dep)

    expected = "sha256=" + hmac.new(
        hmac_secret.encode(),
        ts.encode() + b"." + body,
        "sha256",
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="invalid signature",
        )

    try:
        payload_obj = _json.loads(body)
    except _json.JSONDecodeError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="malformed JSON body",
        )
    inputs = {"payload": payload_obj}

    async with session_scope(tenant_id=tenant_id) as session:
        task_id, claimed = await _claim_webhook_receipt(
            session,
            tenant_id=uuid.UUID(tenant_id),
            deployment_id=uuid.UUID(str(dep["id"])),
            signature=sig,
        )
        if claimed:
            # Invalid signatures and already accepted deliveries must not
            # consume the deployment owner's rate-limit budget.
            await check_rate_limit(dep)
            svc = DeploymentsService(session, DeploymentsRepo(session))
            await svc.submit(
                deployment=dep,
                payload=inputs,
                source="webhook",
                invocation_id=task_id,
            )
    if claimed:
        await bump_redis_invoke_counter(dep["id"])
    return {"task_id": str(task_id)}
