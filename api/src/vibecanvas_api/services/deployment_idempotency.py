"""Atomically bind an optional client key to one admitted invocation.

The receipt, invocation and encrypted history commit together. Concurrent retries
wait for that transaction; rejected admission rolls back the receipt as well.
Payload equality is checked against encrypted history, not a plaintext hash.
"""

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.services.deployment_results import accepted_response, sync_result_response
from vibecanvas_api.storage.workflow_history_repo import TERMINAL_STATUSES, WorkflowHistoryRepo


def _canonical(inputs):
    return json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


async def claim_invocation(session, *, deployment, key: str | None, inputs: dict):
    if key is None:
        return None, True
    if not key or len(key) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise HTTPException(422, "invalid_idempotency_key")
    params = {
        "tenant": uuid.UUID(str(deployment["tenant_id"])),
        "deployment": uuid.UUID(str(deployment["id"])),
        "digest": hashlib.sha256(key.encode("ascii")).hexdigest(),
        "invocation": uuid.uuid4(),
    }
    claimed = await session.scalar(
        text("""INSERT INTO deployment_idempotency_receipts
        (tenant_id, deployment_id, key_digest, invocation_id)
        VALUES (:tenant, :deployment, :digest, :invocation)
        ON CONFLICT (tenant_id, deployment_id, key_digest) DO NOTHING
        RETURNING invocation_id"""),
        params,
    )
    if claimed is not None:
        return claimed, True
    existing = await session.scalar(
        text("""SELECT invocation_id FROM deployment_idempotency_receipts
        WHERE tenant_id=:tenant AND deployment_id=:deployment AND key_digest=:digest"""),
        params,
    )
    detail = await WorkflowHistoryRepo(session).detail(str(existing))
    if detail is None:
        raise RuntimeError("Idempotency receipt has no execution history")
    if _canonical(detail["inputs"]) != _canonical(inputs):
        raise HTTPException(409, "idempotency_key_conflict")
    return existing, False


async def replay_response(session, *, slug: str, invocation_id, asynchronous: bool):
    history = WorkflowHistoryRepo(session)
    run = await history.get(str(invocation_id))
    encountered_approval = await session.scalar(
        text("""SELECT EXISTS (
        SELECT 1 FROM workflow_execution_approvals WHERE execution_id=:id)"""),
        {"id": invocation_id},
    )
    if not asynchronous and not encountered_approval:
        if run["status"] in TERMINAL_STATUSES:
            invocation_status = await session.scalar(text(
                "SELECT status FROM deployment_invocations WHERE id=:id"
            ), {"id": invocation_id})
            if invocation_status is None or invocation_status in TERMINAL_STATUSES:
                return sync_result_response(await history.result_detail(str(invocation_id)))
        return None  # Caller observes after the admission transaction closes.
    return accepted_response(slug=slug, invocation_id=str(invocation_id), state=run["status"],
                             async_reason="explicit_async" if asynchronous else "human_approval")
