"""Durable revision selection. Admission and promotion lock the SAME deployment row.

Only non-secret execution metadata is stored here; graph content stays in the
existing encrypted workflow-version and invocation stores.
"""
from __future__ import annotations

import json
import uuid
from fastapi import HTTPException
from sqlalchemy import text

EXECUTION_FIELDS = ("workflow_tenant_id", "wf_id", "version_pin", "pinned_major", "pinned_sub", "mount_enabled", "service_account_id", "cpu_millis", "memory_mb", "worker_count", "worker_concurrency")


def desired_key(dep: dict) -> str:
    return json.dumps({k: str(dep[k]) if k in {"service_account_id", "workflow_tenant_id"} and dep.get(k) else dep.get(k)
                       for k in EXECUTION_FIELDS}, sort_keys=True)


def revision_scope(revision_id: str) -> str:
    return f"deployment-{uuid.UUID(str(revision_id)).hex}"


def execution_spec(dep: dict, workflow: dict, user_id: str) -> dict:
    meta = workflow["__meta__"]
    return {"wf_id": dep["wf_id"], "workflow_tenant_id": str(dep["workflow_tenant_id"]), "version_pin": "specific",
            "pinned_major": meta["workflow_version"], "pinned_sub": meta["workflow_subversion"],
            "mount_enabled": bool(dep["mount_enabled"]), "user_id": str(user_id),
            "cpu_millis": dep.get("cpu_millis", 500), "memory_mb": dep.get("memory_mb", 256),
            "worker_count": dep.get("worker_count", 1),
            "worker_concurrency": dep.get("worker_concurrency", -1),
            "service_account_id": str(dep["service_account_id"]), "desired_key": desired_key(dep)}


async def admit_revision(session, deployment_id) -> tuple[dict, dict]:
    """Caller creates the invocation in this transaction before releasing lock.

    This closes the select-old / switch / drain / enqueue-old race. A queued
    invocation counts toward draining just like a running one.
    """
    tenant_id = await session.scalar(text("SELECT tenant_id FROM deployments WHERE id=:id"), {"id": deployment_id})
    if tenant_id is None:
        raise HTTPException(404, "deployment not found")
    # Serialize admission across this tenant's deployments before locking the
    # individual deployment. Counts and invocation insertion share one commit;
    # neither Redis availability nor a background queue may bypass capacity.
    tenant = (await session.execute(text("""SELECT max_concurrent_deployments FROM tenants
        WHERE tenant_id=:tenant FOR UPDATE"""), {"tenant": tenant_id})).mappings().one()
    dep = (await session.execute(text("SELECT * FROM deployments WHERE id=:id FOR UPDATE"),
                                 {"id": deployment_id})).mappings().one_or_none()
    if dep is None or dep["deleted_at"] is not None or not dep["enabled"]:
        raise HTTPException(404, "deployment not found")
    if not dep["active_revision_id"]:
        raise HTTPException(503, "deployment_starting", headers={"Retry-After": "2"})
    rev = (await session.execute(text("SELECT * FROM deployment_runtime_revisions WHERE id=:id AND deployment_id=:dep"),
                                 {"id": dep["active_revision_id"], "dep": deployment_id})).mappings().one()
    if rev["state"] != "active":
        raise HTTPException(503, "deployment_not_ready", headers={"Retry-After": "2"})
    counts = (await session.execute(text("""SELECT count(*) AS tenant_count,
        count(*) FILTER (WHERE deployment_id=:id) AS deployment_count
        FROM deployment_invocations WHERE tenant_id=:tenant
        AND status IN ('queued','running','waiting_approval')"""),
        {"id": deployment_id, "tenant": tenant_id})).mappings().one()
    limit = int(rev["spec"].get("worker_count", 1)) * int(rev["spec"].get("worker_concurrency", -1))
    if rev["spec"].get("worker_concurrency", -1) != -1 and counts["deployment_count"] >= limit:
        raise HTTPException(429, "concurrency_limit_exceeded", headers={"Retry-After": "1"})
    if tenant["max_concurrent_deployments"] is not None and counts["tenant_count"] >= tenant["max_concurrent_deployments"]:
        raise HTTPException(429, "tenant_concurrency_limit_exceeded", headers={"Retry-After": "1"})
    return dict(dep), dict(rev)


async def runtime_summary(session, deployment_id) -> dict:
    rows = (await session.execute(text("""SELECT r.id, r.state, r.spec, r.created_at, r.activated_at,
        CASE WHEN r.observed_at > now() - interval '30 seconds' THEN r.runtime_metrics END AS metrics,
        r.observed_at,
        (SELECT count(*) FROM deployment_invocations i WHERE i.revision_id=r.id
            AND i.status IN ('queued','running','waiting_approval')) AS pending_requests
        FROM deployment_runtime_revisions r WHERE r.deployment_id=:id
        AND r.state IN ('active','preparing','draining') ORDER BY r.created_at"""),
        {"id": deployment_id})).mappings().all()
    return {"instances": [{"id": str(r["id"]), "state": r["state"],
                            "version": f"v{r['spec']['pinned_major']}.sv{r['spec']['pinned_sub']}",
                            "mount_enabled": r["spec"]["mount_enabled"],
                            "worker_count": r["spec"].get("worker_count", 1),
                            "worker_concurrency": r["spec"].get("worker_concurrency", -1),
                            "created_at": r["created_at"].isoformat(),
                            "activated_at": r["activated_at"].isoformat() if r["activated_at"] else None,
                            "metrics": r["metrics"],
                            "observed_at": r["observed_at"].isoformat() if r["observed_at"] else None,
                            "pending_requests": r["pending_requests"]} for r in rows]}
