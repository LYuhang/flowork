"""External deployment admission and result observation.

Invoke waits briefly for the resident sandbox. A reached human approval or an
expired HTTP wait returns 202 for the same execution. Durable asynchronous
submission uses the existing queue; neither path may bypass capacity admission.
API keys are checked before binding tenant scope, and all result queries are
restricted to the authenticated deployment.
"""
from __future__ import annotations

from vibecanvas_api.services.deployment_completion import complete_before_cancelling
from vibecanvas_api.services.deployment_idempotency import claim_invocation, replay_response
from vibecanvas_api.services.deployment_http import DeploymentPublicRoute

import hashlib
import uuid
from typing import Annotated, Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import text

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
from vibecanvas_api.services.deployment_dispatch import dispatch_invocation as _dispatch_invocation
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/deployments", tags=["deployments-invoke"],
    route_class=DeploymentPublicRoute,
)

_WEBHOOK_MAX_BODY_BYTES = 1_048_576
_WEBHOOK_REPLAY_RETENTION_SECONDS = 600


@router.get("/{slug}/runs/{invocation_id}")
async def get_invocation_result(
    slug: str, invocation_id: uuid.UUID,
    authorization: Optional[str] = Header(default=None),
):
    """An authorized poll returns 200 even when the execution itself failed."""
    api_key = _extract_bearer(authorization)
    if api_key is None:
        raise HTTPException(401, "Bearer token required")
    dep = await resolve_deployment_and_bind_tenant(api_key=api_key)
    # Disabling admission does not erase existing results. Rotation/revocation
    # still takes effect through the same current-key lookup on every poll.
    if dep is None or dep["slug"] != slug or dep["trigger_type"] != "api":
        raise HTTPException(404, "deployment not found")
    return await _invocation_result(dep, invocation_id)


async def _invocation_result(dep: dict, invocation_id: uuid.UUID):
    from vibecanvas_api.services.deployment_results import external_result
    from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo

    async with session_scope(tenant_id=str(dep["tenant_id"])) as session:
        history = WorkflowHistoryRepo(session)
        run = await history.get(str(invocation_id))
        if run is None or run["source_type"] != "deployment" or run["source_id"] != str(dep["id"]):
            raise HTTPException(404, "invocation_not_found")
        return external_result(await history.result_detail(str(invocation_id)))


@router.get("/{slug}/webhook/runs/{invocation_id}")
async def get_webhook_invocation_result(slug: str, invocation_id: uuid.UUID, request: Request):
    """Read with the current webhook secret, signing timestamp + '.GET ' + path.

    Bind the signature to method, deployment and invocation; a signed POST body
    or another invocation's polling signature cannot authorize this lookup.
    """
    import hmac
    import time

    from vibecanvas_api.services.deployment_results import webhook_result_location

    timestamp = request.headers.get("X-Vibecanvas-Timestamp", "")
    signature = request.headers.get("X-Vibecanvas-Signature", "")
    try:
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(401, "invalid or expired timestamp") from None
    dep = await resolve_deployment_and_bind_tenant(slug=slug)
    # Disabled admission does not revoke access to retained execution results.
    if dep is None or dep["trigger_type"] != "webhook":
        raise HTTPException(404, "deployment not found")
    async with session_scope(tenant_id=str(dep["tenant_id"])) as session:
        secret = await resolve_deployment_hmac_secret(session, dep)
    path = webhook_result_location(slug, str(invocation_id))
    message = f"{timestamp}.GET {path}".encode()
    expected = "sha256=" + hmac.new(secret.encode(), message, "sha256").hexdigest()
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise HTTPException(401, "invalid signature")
    return await _invocation_result(dep, invocation_id)


async def _read_body_with_hard_limit(request: Request, *, limit: int) -> bytes:
    """Read at most ``limit`` actual bytes, independent of client headers."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"payload too large (max {limit} bytes)",
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
@complete_before_cancelling
async def invoke_sync(
    slug: str,
    body: dict,
    authorization: Optional[str] = Header(default=None),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description=(
            "Optional per-deployment operation key (1–256 visible ASCII characters). "
            "Reusing a key with identical input returns the same invocation; changed input returns 409. "
            "Shared by /invoke and /runs; retained with history."
        )),
    ] = None,
):
    """Run synchronously until completion, human approval or the HTTP wait limit."""
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

    tenant_id = str(tenant_id_var.get())

    async with session_scope(tenant_id=tenant_id) as session:
        receipt_id, fresh = await claim_invocation(session, deployment=dep, key=idempotency_key, inputs=body)
        if not fresh:
            replay = await replay_response(session, slug=slug, invocation_id=receipt_id, asynchronous=False)
            if replay is not None:
                return replay
            await session.commit()
            from vibecanvas_api.services.deployment_observer import observe_invocation
            return await observe_invocation(tenant_id=tenant_id, slug=slug, invocation_id=str(receipt_id))
        await check_rate_limit(dep)
        from vibecanvas_api.services.deployment_revisions import admit_revision
        from vibecanvas_api.services.deployment_snapshots import resolve_workflow
        dep, revision = await admit_revision(session, dep["id"])
        workflow_dict = await resolve_workflow(session, dep["user_id"], revision["spec"])
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
            invocation_id=receipt_id,
            tenant_id=uuid.UUID(tenant_id),
            deployment_id=dep["id"],
            wf_id=dep["wf_id"],
            trigger_type=dep["trigger_type"],
            source="sync_api",
            status="running",
            revision_id=revision["id"],
        )
        from vibecanvas_api.services.deployment_execution_history import create_deployment_history
        await create_deployment_history(
            session, invocation_id=invocation_id, deployment=dep, revision=revision,
            workflow=workflow_dict, inputs=body,
        )

    from vibecanvas_api.services.deployment_observer import own_dispatch, observe_invocation
    dispatch = own_dispatch(_dispatch_invocation(
        dep=dep, revision=revision, lease=lease, workflow=workflow_dict,
        inputs=body, tenant_id=tenant_id, invocation_id=str(invocation_id),
    ))
    await bump_redis_invoke_counter(dep["id"])
    return await observe_invocation(
        tenant_id=tenant_id, slug=slug, invocation_id=str(invocation_id), dispatch=dispatch,
    )




@router.post("/{slug}/runs", status_code=status.HTTP_202_ACCEPTED)
async def invoke_async(
    slug: str,
    body: dict,
    authorization: Optional[str] = Header(default=None),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description=(
            "Optional per-deployment operation key (1–256 visible ASCII characters). "
            "Reusing a key with identical input returns the same invocation; changed input returns 409. "
            "Shared by /invoke and /runs; retained with history."
        )),
    ] = None,
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

    tenant_id = tenant_id_var.get()
    async with session_scope(tenant_id=str(tenant_id)) as session:
        receipt_id, fresh = await claim_invocation(session, deployment=dep, key=idempotency_key, inputs=body)
        if not fresh:
            return await replay_response(session, slug=slug, invocation_id=receipt_id, asynchronous=True)
        await check_rate_limit(dep)
        svc = DeploymentsService(session, DeploymentsRepo(session))
        task_id = await svc.submit(
            deployment=dep,
            payload=body,
            source="async_api",
            invocation_id=receipt_id,
        )
    await bump_redis_invoke_counter(dep["id"])
    from vibecanvas_api.services.deployment_results import accepted_response
    return accepted_response(slug=slug, invocation_id=str(task_id))


@router.post("/{slug}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def webhook(slug: str, request: Request):
    """Spec §6.3 — webhook receiver with HMAC verification + size guard.

    No Bearer auth: trust comes from a valid signature over
    ``timestamp + "." + raw_body`` using the deployment's ``hmac_secret``.
    Returns 202 with invocation_id, task_id and a signed-query result URL.

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
            status.HTTP_413_CONTENT_TOO_LARGE,
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

    receipt_state = "queued"
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
        else:
            from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo

            run = await WorkflowHistoryRepo(session).get(str(task_id))
            if run is None:
                raise RuntimeError("Webhook receipt has no execution history")
            receipt_state = run["status"]
    if claimed:
        await bump_redis_invoke_counter(dep["id"])
    from vibecanvas_api.services.deployment_results import accepted_response
    return accepted_response(slug=slug, invocation_id=str(task_id), state=receipt_state, webhook=True)
