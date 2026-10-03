"""Shared, transactional Workflow deletion; cleanup is durable and retryable."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

from sqlalchemy import or_, select, text

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.audit import actions
from vibecanvas_api.audit.service import record_audit
from vibecanvas_api.authorization.projection import enqueue_structural_delta, resource_root_edges
from vibecanvas_api.services.background_queue import enqueue_background_job_in_transaction
from vibecanvas_api.storage.models import Chat, ChatProject, Workflow, VfsArtifact, VfsScratch, VfsRun
from vibecanvas_api.storage.models_agent_runs import AgentRun, ACTIVE_AGENT_RUN_STATUSES
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


async def lock_live_workflow(session, workflow_id):
    row = (await session.execute(select(Workflow).where(Workflow.wf_id == workflow_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None or row.deleted_at is not None:
        raise ToolError("workflow_unavailable", "The workflow does not exist or is no longer available.")
    return row


async def check_dependencies(session, workflow_id):
    # Dependency producers take this Workflow row lock via database triggers.
    checks = (
        ("deployments", "wf_id", "enabled AND deleted_at IS NULL", "enabled deployments"),
        ("task_schedules", "workflow_id", "enabled", "enabled scheduled tasks"),
        ("tasks", "workflow_id", "status IN ('queued','running','cancelling','resuming')", "active tasks"),
        ("deployment_invocations", "wf_id", "status IN ('queued','running','waiting_approval')", "active deployment invocations"),
        ("scheduled_run_executions", "workflow_id", "status IN ('queued','running','cancelling')", "active scheduled executions"),
        ("workflow_run_state", "wf_id", "status IN ('pending','running')", "active canvas executions"),
        # A lost heartbeat is not proof that a worker stopped. Only confirmed
        # pool shutdown removes this record; uncertain runs block deletion.
        ("workflow_cli_leases", "workflow_id", "operation = 'run'", "active or unconfirmed CLI executions"),
    )
    for table, column, condition, label in checks:
        if (await session.execute(text(f"SELECT 1 FROM {table} WHERE {column}=:id AND {condition} LIMIT 1"),
                                  {"id": workflow_id})).first():
            raise ToolError("workflow_in_use", f"The workflow has {label}.",
                            info={"hint": "Disable scheduled dependencies and cancel or finish active executions before retrying."})

    active_chat = (await session.execute(select(AgentRun.run_id)
        .join(Chat, Chat.chat_id == AgentRun.chat_id)
        .join(ChatProject, ChatProject.project_id == Chat.project_id)
        .where(ChatProject.workflow_id == workflow_id,
               AgentRun.status.in_(ACTIVE_AGENT_RUN_STATUSES)).limit(1))).first()
    if active_chat:
        raise ToolError("workflow_in_use", "The workflow has active canvas conversations.",
                        info={"hint": "Stop or finish the active conversations before deleting the Workflow."})

    from vibecanvas_api.storage.background_delivery_repo import BackgroundDeliveryRepo
    for chat in await linked_chats(session, workflow_id):
        if await BackgroundDeliveryRepo(session).list_sandbox_holds_for_user(
            chat_id=chat.chat_id, creator_user_id=str(chat.creator_user_id), limit=1,
        ):
            raise ToolError("workflow_in_use", "A canvas background job still needs this Workflow sandbox.")


async def linked_chats(session, workflow_id):
    return (await session.execute(select(Chat).join(ChatProject,
        ChatProject.project_id == Chat.project_id).where(
            ChatProject.workflow_id == workflow_id, Chat.deleted_at.is_(None)))).scalars().all()


def fingerprint(meta):
    # Covers all version/meta edits, not only global HEAD switches.
    return hashlib.sha256(json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest()


async def preflight(session, workflow_id, user_id):
    await lock_live_workflow(session, workflow_id)
    await check_dependencies(session, workflow_id)
    meta = await WorkflowRepo(session, user_id).get_meta(workflow_id)
    return meta


async def commit_deletion(session, *, workflow_id, user_id, tenant_id, coordinator,
                          expected=None, authorize=None,
                          audit_ctx=None, actor_email=""):
    meta = await preflight(session, workflow_id, user_id)
    if expected is not None and fingerprint(meta) != expected:
        raise ToolError("workflow_changed", "The workflow changed while approval was pending.",
                        info={"hint": "Review the updated workflow and request approval again."})
    if authorize is not None:
        await authorize()
    from vibecanvas_api.storage.chat_repo import ChatRepo
    from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
    chats = await linked_chats(session, workflow_id)
    chat_edges = frozenset().union(*(resource_root_edges(
        organization_id=str(tenant_id), object_type="chat", object_id=chat.chat_id,
        owner_relation="creator", owner_type="user", owner_id=str(chat.creator_user_id),
    ) for chat in chats))
    for chat in chats:
        await ChatRepo(session, str(chat.creator_user_id)).drop_authorized_session(chat.chat_id)
    projects = (await session.execute(select(ChatProject).where(
        ChatProject.workflow_id == workflow_id, ChatProject.deleted_at.is_(None)))).scalars().all()
    for project in projects:
        await ChatProjectRepo(session, str(project.creator_user_id)).soft_delete(project.project_id)
    await WorkflowRepo(session, user_id).delete_workflow(workflow_id)
    await session.execute(text("""INSERT INTO workflow_deletion_cleanup(workflow_id,tenant_id)
        VALUES (:id,CAST(:tenant AS uuid))"""), {"id": workflow_id, "tenant": str(tenant_id)})
    mutations = await enqueue_structural_delta(session=session, coordinator=coordinator,
        actor_type="user", actor_id=str(user_id), before=resource_root_edges(
            organization_id=str(tenant_id), object_type="workflow", object_id=workflow_id,
            owner_relation="manager", owner_type="user", owner_id=str(meta["owner_id"])) | chat_edges,
        after=frozenset(), operation_id=uuid.uuid4().hex, source="workflow-delete")
    await record_audit(session, action=actions.WORKFLOW_DELETE, actor_user_id=user_id,
        actor_email=actor_email, target_type=actions.TARGET_WORKFLOW, target_id=workflow_id,
        target_name=meta.get("workflow_name"), outcome="success", audit_ctx=audit_ctx,
        meta={"cleanup": "pending"})
    await enqueue_background_job_in_transaction(session, "workflow.delete_cleanup",
        job_id=f"workflow-delete:{workflow_id}", queue="maintenance", kwargs={"workflow_id": workflow_id})
    await session.commit()
    return {"id": workflow_id, "deleted": True,
            "cleanup": "pending", "message": "Workflow deleted. Resource cleanup is pending."}, mutations


async def cleanup(workflow_id):
    from vibecanvas_api.storage.sync_session import short_admin_session
    from vibecanvas_api.services.object_store import get_object_store
    from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
    async with short_admin_session() as session:
        tenant = (await session.execute(text("SELECT tenant_id FROM workflow_deletion_cleanup WHERE workflow_id=:id"),
                                       {"id": workflow_id})).scalar_one_or_none()
    if tenant is None:
        return
    async with short_admin_session() as session:
        ledger = (await session.execute(text("SELECT completed_at FROM workflow_deletion_cleanup WHERE workflow_id=:id FOR UPDATE"),
                                       {"id": workflow_id})).one()
        if ledger.completed_at is not None:
            return
        row = await session.get(Workflow, workflow_id)
        if row is None or row.deleted_at is None:
            raise RuntimeError("Refusing cleanup of a live workflow")
        closed = await get_sandbox_manager().close_session(str(tenant), workflow_id)
        if closed.get("lifecycle_state") not in {"closed", "released"}:
            raise RuntimeError("Workflow sandbox shutdown/persistence is not confirmed")
        store = get_object_store()
        # Delete objects BEFORE removing their durable index. A crash/retry
        # reuses the same exact keys. Chat files share this Workflow scope;
        # never delete another Workflow or the user-owned /mount scope.
        for model in (VfsArtifact, VfsScratch, VfsRun):
            criterion = model.wf_id == workflow_id if model is VfsRun else (
                (model.scope_id == workflow_id) & or_(*(
                    (model.path == prefix) | model.path.startswith(prefix + "/")
                    for prefix in ("/data", "/memory", "/logs", "/chats", "/__runtime")
                )))
            rows = (await session.execute(select(model).where(criterion))).scalars().all()
            for item in rows:
                if item.object_key:
                    await asyncio.to_thread(store.delete_bytes, item.object_key)
                    prefix = item.object_key.rsplit("/", 1)[0] + "/"
                    if item.object_key in await asyncio.to_thread(store.list_keys, prefix):
                        raise RuntimeError("Workflow object cleanup was not confirmed")
                await session.delete(item)
        from vibecanvas_api.services.vfs_volume import get_project_runtime_volume_provider
        owners = (await session.execute(select(ChatProject.creator_user_id).where(
            ChatProject.workflow_id == workflow_id))).scalars().all()
        for owner in set(owners):
            await asyncio.to_thread(get_project_runtime_volume_provider().delete,
                tenant_id=str(tenant), user_id=str(owner), project_scope_id=workflow_id)
        await session.execute(text("UPDATE workflow_deletion_cleanup SET completed_at=now() WHERE workflow_id=:id"),
                              {"id": workflow_id})


def run_cleanup(*, workflow_id):
    asyncio.run(cleanup(workflow_id))
