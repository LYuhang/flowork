"""Deletion fence for live CLI worker pools, not a Workflow selection."""
from sqlalchemy import text

from vibecanvas_api.services.agent_resources.authorization import _require_active_chat_write
from vibecanvas_api.services.workflow_deletion import lock_live_workflow
from vibecanvas_api.storage.db import session_scope


async def reserve(ctx, run_id, workflow_id):
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
        await _require_active_chat_write(session, ctx)
        await lock_live_workflow(session, workflow_id)
        await session.execute(text("""INSERT INTO workflow_cli_leases
            (call_id,tenant_id,workflow_id,run_id,operation,expires_at)
            VALUES (:id,CAST(:tenant AS uuid),:wf,:turn,'run',now()+interval '30 seconds')"""),
            {"id": run_id, "tenant": ctx.tenant_id, "wf": workflow_id, "turn": ctx.turn_id})


async def renew(tenant_id, run_id):
    async with session_scope(tenant_id=tenant_id) as session:
        result = await session.execute(text("""UPDATE workflow_cli_leases
            SET expires_at=now()+interval '30 seconds' WHERE call_id=:id AND expires_at>now() RETURNING call_id"""), {"id": run_id})
        return result.scalar_one_or_none() is not None


async def release(tenant_id, run_id):
    async with session_scope(tenant_id=tenant_id) as session:
        await session.execute(text("DELETE FROM workflow_cli_leases WHERE call_id=:id"), {"id": run_id})
