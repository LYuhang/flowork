"""``DeploymentsService`` — single submit() path + tenant resolver.

Deployments T2. Spec §6 / §11.

Two distinct responsibilities, deliberately co-located so external-flow
route handlers import one module:

1. ``resolve_deployment_and_bind_tenant`` — admin-role lookup that binds
   ``tenant_id_var`` from the deployment row. EVERY external endpoint
   (``api/<slug>/invoke`` and ``webhook/<slug>``) MUST call
   this BEFORE any tenant-bound DB op. The bound tenant is then read by
   the route handler and forwarded explicitly to
   ``session_scope(tenant_id=...)`` for the rest of the request.

2. ``DeploymentsService.submit`` — the single path through which a
   deployment dispatches durable background work. It returns an invocation id
   and enqueues a ``deployment_invoke`` workflow; Deployment-specific
   logs/history own observability, not the Task Center table. The queue comes from
   ``route_for("deployment_invoke", deployment["id"])`` (Deployments
   T3) — future multi-cluster routing extends the helper, not this
   call site.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.services.background_queue import (
    enqueue_background_job_in_transaction,
)
from vibecanvas_api.services.queue_routing import route_for
from vibecanvas_api.services.tenant_db import (
    session_scope_admin, tenant_id_var,
)
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo


async def resolve_deployment_and_bind_tenant(
    *,
    slug: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[dict]:
    """Admin-role lookup of a deployment; bind ``tenant_id_var``; return
    the row.

    Spec §6 hard invariant: every external endpoint MUST call this
    BEFORE any tenant-scoped DB op. Failure to do so means the
    subsequent ``session_scope(tenant_id=...)`` would either receive a
    stale CV or no value, and Postgres FORCE RLS would silently filter
    every row.

    Returns ``None`` for an unknown slug / api_key. The caller is
    expected to translate that into:

    * 404 for an ``api_key`` miss (the api flow MUST authenticate);
    * 404 for a slug miss (webhook / public invoke).

    If both ``slug`` and ``api_key`` are ``None``, returns ``None``
    without a DB roundtrip — defends against an upstream parse bug.
    """
    if slug is None and api_key is None:
        return None
    async with session_scope_admin() as s:
        repo = DeploymentsRepo(s)
        dep: Optional[dict] = None
        # api_key takes precedence: an api caller authenticates BY key,
        # the slug is informational.
        if api_key is not None:
            dep = await repo.get_by_api_key(api_key)
        if dep is None and slug is not None:
            dep = await repo.get_by_slug_admin(slug)
    if dep is None:
        return None
    tenant_id_var.set(dep["tenant_id"])
    return dep


class DeploymentsService:
    """Submit work for a deployment.

    Single entry point so future multi-cluster routing changes only this
    file (forward-compat #4 in the spec).

    Caller contract:
    * ``session`` is already tenant-bound to ``deployment['tenant_id']``
      — typically opened via ``session_scope(tenant_id=...)`` AFTER
      ``resolve_deployment_and_bind_tenant`` returned the row.
    * ``deployment`` is the ``mappings()`` row dict returned by
      ``DeploymentsRepo`` (NOT an ORM ``Deployment``).
    """

    def __init__(self, session: AsyncSession, repo: DeploymentsRepo):
        self.session = session
        self.repo = repo

    async def submit(
        self,
        *,
        deployment: dict,
        payload: dict,
        source: str,
        invocation_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Durably dispatch a ``deployment_invoke`` background workflow."""
        task_id = invocation_id or uuid.uuid4()
        inv_repo = DeploymentInvocationsRepo(self.session)
        await inv_repo.create(
            invocation_id=task_id,
            tenant_id=deployment["tenant_id"],
            deployment_id=deployment["id"],
            wf_id=deployment["wf_id"],
            trigger_type=deployment["trigger_type"],
            source=source,
            status="queued",
            inputs=payload,
        )
        try:
            await enqueue_background_job_in_transaction(
                self.session,
                "deployment_invoke",
                job_id=str(task_id),
                queue=route_for("deployment_invoke", deployment["id"]),
                kwargs={"invocation_id": str(task_id)},
            )
        except Exception as exc:
            await inv_repo.mark_terminal(
                task_id,
                status="failed",
                latency_ms=None,
                error=f"enqueue_failed:{type(exc).__name__}",
            )
            raise
        return task_id
