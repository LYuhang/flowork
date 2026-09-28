"""Host-owned destructive command with durable, live-command-only approval."""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.mutations import AuthzMutationCoordinator
from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.authorization.projection import apply_committed_structural_mutations
from vibecanvas_api.flowork_cli.cli import error, uncertain_result
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.authorization import require_workflow_action, _require_active_chat_write
from vibecanvas_api.services import workflow_deletion as deletion
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo


async def renew(call):
    table = "knowledge_cli_leases" if call.operation.startswith("knowledge.") else ("deployment_cli_leases" if call.operation.startswith("deployment.") else ("task_cli_leases" if call.operation.startswith("task.") else "workflow_cli_leases"))
    async with session_scope(tenant_id=call.capability.tenant_id) as session:
        result = await session.execute(text(f"""UPDATE {table} SET expires_at=now()+interval '30 seconds'
            WHERE call_id=:id AND expires_at > now() RETURNING call_id"""), {"id": call.call_id})
        if result.scalar_one_or_none() is None:
            raise PermissionError("CLI lease expired")


async def release(call):
    table = "knowledge_cli_leases" if call.operation.startswith("knowledge.") else ("deployment_cli_leases" if call.operation.startswith("deployment.") else ("task_cli_leases" if call.operation.startswith("task.") else "workflow_cli_leases"))
    async with session_scope(tenant_id=call.capability.tenant_id) as session:
        if call.hitl_id:
            await HitlRepo(session).resolve(hitl_request_id=call.hitl_id, decision="cancel",
                decision_payload={"reason": "CLI command ended."})
        await session.execute(text(f"DELETE FROM {table} WHERE call_id=:id"), {"id": call.call_id})


async def _approve(call, meta, *, prompt=None):
    from .orchestrator import AgentRuntimeOrchestrator
    from .protocol import RuntimeEvent
    cap = call.capability
    hitl_id = "hitl_" + uuid.uuid4().hex
    call.hitl_id = hitl_id
    task_command = call.operation.startswith(("task.", "deployment.", "knowledge."))
    tool_name = "flowork-cli " + call.operation.replace(".", " ")
    title = "Approve Deployment operation" if call.operation.startswith("deployment.") else ("Approve Task operation" if task_command else "Delete Workflow")
    if call.operation.startswith("knowledge."):
        title = "Approve Knowledge operation"
    arguments = meta if task_command else {"workflow_id": meta["wf_id"]}
    prompt = prompt or (f"Delete Workflow {meta.get('workflow_name', '')!r} (ID: {meta['wf_id']})? "
              "All versions will become unavailable. Workflow data and run files will be removed. "
              "Full recovery is not available. Chat files, memory and mounts will not be deleted.")
    event = RuntimeEvent(event_id=uuid.uuid4().hex, runtime_type="codex", runtime_session_id=cap.runtime_session_id,
        chat_id=cap.chat_id, turn_id=cap.turn_id, seq=1, type="approval.required", payload={
            "hitl_request_id": hitl_id, "title": title, "prompt_text": prompt,
            "agent_payload": {"tool": tool_name, "arguments": arguments},
            "runtime_correlation": {"source": "flowork_cli", "runtime_request_id": call.call_id,
                "runtime_method": call.operation, "runtime_item_id": "cli_" + call.call_id}})
    prepared = AgentRuntimeOrchestrator._prepare_approval(event)
    payload = dict(prepared.payload)
    private = payload.pop("_persist")
    definition = private["definition"]
    definition["interaction_schema"].update(submit_label="Confirm" if task_command else "Confirm deletion", cancel_label="Cancel")
    async with session_scope(tenant_id=cap.tenant_id) as session:
        repo = HitlRepo(session)
        await repo.create_interactive_artifact(artifact_id=private["artifact_id"], tenant_id=cap.tenant_id,
            chat_id=cap.chat_id, run_id=cap.turn_id, component_type="approval", completion_mode="wait_for_submit",
            title=payload["title"], definition_json=definition, artifact_ref=None, content_hash=None)
        await repo.create_request(hitl_request_id=hitl_id, tenant_id=cap.tenant_id, chat_id=cap.chat_id,
            run_id=cap.turn_id, artifact_id=private["artifact_id"], hitl_type="pre_tool_approval",
            title=payload["title"], prompt_text=prompt, ui_payload_json={"type": "HITL_REQUIRED", **payload},
            agent_payload_json=private["agent_payload"], runtime_correlation_json=private["runtime_correlation"],
            mark_run_waiting=False)
        await repo.link_artifact_hitl(private["artifact_id"], hitl_id)
    await call.emit({"progress": {"status": "awaiting_approval", "id": meta.get("knowledge_id", meta.get("deployment_id", meta.get("task_id", meta.get("wf_id")))),
        "message": "Waiting for user approval."}, "_cli_events": [
            {"event_type": "HITL_REQUIRED", "payload": payload},
            {"event_type": "CHAT_EVENT", "payload": {"type": "tool_start", "tool_call_id": "cli_" + call.call_id,
                "name": tool_name, "arguments": arguments}},
            {"event_type": "CHAT_EVENT", "payload": payload["projection_event"]}]})
    while True:
        async with session_scope(tenant_id=cap.tenant_id) as session:
            row = await HitlRepo(session).get_request(hitl_id)
            status = row.status if row else "cancelled"
        if status != "pending":
            await call.emit({"_cli_events": [{"event_type": "HITL_RESOLVED", "payload": {
                "hitl_request_id": hitl_id, "status": status}}]})
            if status != "approved":
                raise ToolError("approval_denied" if status == "denied" else "approval_cancelled",
                                "User declined the operation. No changes were made." if status == "denied"
                                else "The approval is no longer active. No changes were made.")
            return
        await asyncio.sleep(0.5)


async def execute(call, arguments):
    cap = call.capability
    workflow_id = arguments["workflow_id"]
    committed = False
    try:
        ctx = await agent_context.resolve_context(cap)
        await require_workflow_action(ctx, workflow_id, Action.DELETE,
                                      consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        async with session_scope(tenant_id=cap.tenant_id) as session:
            meta = await deletion.preflight(session, workflow_id, cap.user_id)
            await session.execute(text("""INSERT INTO workflow_cli_leases(call_id,tenant_id,workflow_id,run_id,operation,expires_at)
                VALUES (:id,CAST(:tenant AS uuid),:wf,:run,'delete',now()+interval '30 seconds')"""),
                {"id": call.call_id, "tenant": cap.tenant_id, "wf": workflow_id, "run": cap.turn_id})
        call.durable_lease = True
        if cap.approval_mode not in {"agent", "always_ask", "always_allow"}:
            raise ToolError("invalid_approval_mode", "Unknown approval mode. No changes were made.")
        if cap.approval_mode != "always_allow":
            await _approve(call, meta)
        await call.emit({"progress": {"status": "auto_approved" if cap.approval_mode == "always_allow" else "approved",
            "id": workflow_id, "message": "Deletion allowed by approval mode: always_allow." if cap.approval_mode == "always_allow"
            else "User approved the deletion. Rechecking permissions and dependencies."}})
        ctx = await agent_context.resolve_context(cap)
        coordinator = AuthzMutationCoordinator(client=ctx.authorization_client, organization_id=cap.tenant_id)
        async with session_scope(tenant_id=cap.tenant_id, user_id=cap.user_id) as session:
            await _require_active_chat_write(session, ctx)
            async def authorize():
                await require_workflow_action(ctx, workflow_id, Action.DELETE,
                                              consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
                live = (await session.execute(text("SELECT 1 FROM workflow_cli_leases WHERE call_id=:id AND expires_at>now()"),
                                              {"id": call.call_id})).first()
                if not live:
                    raise ToolError("approval_cancelled", "The CLI command is no longer active.")
            result, mutations = await deletion.commit_deletion(session, workflow_id=workflow_id,
                user_id=cap.user_id, tenant_id=cap.tenant_id, coordinator=coordinator,
                expected=deletion.fingerprint(meta), authorize=authorize)
            committed = True
        # A projection outage must not turn a committed deletion into a retry.
        try:
            await apply_committed_structural_mutations(coordinator, mutations)
        except Exception:
            pass  # Durable authorization outbox owns retries.
        result["_cli_events"] = [{"event_type": "HISTORY_SYNC", "payload": {}}]
        return result
    except ToolError as exc:
        return error(str(exc), exc.message, (exc.info or {}).get("hint", "Check permissions and dependencies before retrying."))
    except PermissionError:
        return error("permission_denied", "The identity or command is no longer authorized.", "Check access before retrying.")
    except Exception:
        if committed:
            return {"id": workflow_id, "deleted": True, "cleanup": "pending", "message": "Workflow deleted. Resource cleanup is pending."}
        return uncertain_result()
