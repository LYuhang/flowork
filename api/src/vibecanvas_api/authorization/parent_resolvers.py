"""Explicit child-resource to authorization-root resolver registry."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import uuid

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.storage.models import VfsRun, WorkflowRunState
from vibecanvas_api.storage.models_agent_runs import (
    AgentRun,
    HitlRequest,
    InteractiveArtifact,
)
from vibecanvas_api.storage.models_background_jobs import ChatToolJob
from vibecanvas_api.storage.models_kb import KbFile
from vibecanvas_api.storage.models_skills import SkillRevision
from vibecanvas_api.storage.models_tasks import (
    ScheduledRunExecution,
    TaskSchedule,
)

from .types import ResourceRef, ResourceType

ParentResolver = Callable[
    [AsyncSession, ResourceRef],
    Awaitable[ResourceRef | None],
]


def _root(resource_type: ResourceType, resource: ResourceRef, root_id: str) -> ResourceRef:
    return ResourceRef(resource_type, root_id, resource.organization_id)


async def _agent_run_parent(
    session: AsyncSession, resource: ResourceRef,
) -> ResourceRef | None:
    chat_id = (
        await session.execute(
            select(AgentRun.chat_id).where(AgentRun.run_id == resource.id)
        )
    ).scalar_one_or_none()
    return _root(ResourceType.CHAT, resource, chat_id) if chat_id else None


async def _hitl_parent(
    session: AsyncSession, resource: ResourceRef,
) -> ResourceRef | None:
    chat_id = (
        await session.execute(
            select(HitlRequest.chat_id).where(
                HitlRequest.hitl_request_id == resource.id
            )
        )
    ).scalar_one_or_none()
    return _root(ResourceType.CHAT, resource, chat_id) if chat_id else None


async def _artifact_parent(
    session: AsyncSession, resource: ResourceRef,
) -> ResourceRef | None:
    chat_id = (
        await session.execute(
            select(InteractiveArtifact.chat_id).where(
                InteractiveArtifact.artifact_id == resource.id
            )
        )
    ).scalar_one_or_none()
    return _root(ResourceType.CHAT, resource, chat_id) if chat_id else None


async def _background_job_parent(
    session: AsyncSession, resource: ResourceRef,
) -> ResourceRef | None:
    chat_id = (
        await session.execute(
            select(ChatToolJob.chat_id).where(ChatToolJob.job_id == resource.id)
        )
    ).scalar_one_or_none()
    return _root(ResourceType.CHAT, resource, chat_id) if chat_id else None


async def _browser_binding_parent(
    session: AsyncSession, resource: ResourceRef,
) -> ResourceRef | None:
    del session
    # Browser-control state is stored directly on the Chat row, so its stable
    # child identifier is the Chat id itself.
    return _root(ResourceType.CHAT, resource, resource.id)


async def _vfs_run_parent(
    session: AsyncSession, resource: ResourceRef,
) -> ResourceRef | None:
    chat_id = (
        await session.execute(
            select(AgentRun.chat_id).where(AgentRun.run_id == resource.id)
        )
    ).scalar_one_or_none()
    if chat_id:
        return _root(ResourceType.CHAT, resource, chat_id)
    workflow_id = (
        await session.execute(
            select(VfsRun.wf_id).where(
                VfsRun.run_id == resource.id,
                VfsRun.wf_id.is_not(None),
            ).limit(1)
        )
    ).scalar_one_or_none()
    return (
        _root(ResourceType.WORKFLOW, resource, workflow_id)
        if workflow_id
        else None
    )


async def _workflow_execution_parent(
    session: AsyncSession,
    resource: ResourceRef,
) -> ResourceRef | None:
    workflow_id = (
        await session.execute(
            select(WorkflowRunState.wf_id).distinct().where(or_(
                WorkflowRunState.turn_id == resource.id,
                WorkflowRunState.wf_id == resource.id,
            ))
        )
    ).scalar_one_or_none()
    return (
        _root(ResourceType.WORKFLOW, resource, workflow_id)
        if workflow_id
        else None
    )


async def _task_execution_parent(
    session: AsyncSession,
    resource: ResourceRef,
) -> ResourceRef | None:
    task_id = (
        await session.execute(
            select(TaskSchedule.task_id)
            .join(
                ScheduledRunExecution,
                ScheduledRunExecution.schedule_id == TaskSchedule.id,
            )
            .where(ScheduledRunExecution.id == resource.id)
        )
    ).scalar_one_or_none()
    return (
        _root(ResourceType.TASK, resource, str(task_id))
        if task_id
        else None
    )


async def _deployment_invocation_parent(
    session: AsyncSession,
    resource: ResourceRef,
) -> ResourceRef | None:
    try:
        invocation_id = uuid.UUID(resource.id)
    except ValueError:
        return None
    deployment_id = (
        await session.execute(
            text(
                "SELECT deployment_id::text FROM deployment_invocations "
                "WHERE id = CAST(:invocation_id AS uuid)"
            ),
            {"invocation_id": str(invocation_id)},
        )
    ).scalar_one_or_none()
    return (
        _root(ResourceType.DEPLOYMENT, resource, deployment_id)
        if deployment_id
        else None
    )


async def _knowledge_base_file_parent(
    session: AsyncSession,
    resource: ResourceRef,
) -> ResourceRef | None:
    try:
        file_id = uuid.UUID(resource.id)
    except ValueError:
        return None
    knowledge_base_id = (
        await session.execute(
            select(KbFile.kb_id).where(
                KbFile.id == file_id,
                KbFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return (
        _root(
            ResourceType.KNOWLEDGE_BASE,
            resource,
            str(knowledge_base_id),
        )
        if knowledge_base_id
        else None
    )


async def _skill_revision_parent(
    session: AsyncSession,
    resource: ResourceRef,
) -> ResourceRef | None:
    try:
        revision_id = uuid.UUID(resource.id)
    except ValueError:
        return None
    skill_id = (
        await session.execute(
            select(SkillRevision.skill_id).where(
                SkillRevision.revision_id == revision_id,
            )
        )
    ).scalar_one_or_none()
    return (
        _root(ResourceType.SKILL_INSTALLATION, resource, str(skill_id))
        if skill_id
        else None
    )


AUTHZ_PARENT_RESOLVERS: dict[ResourceType, ParentResolver] = {
    ResourceType.AGENT_RUN: _agent_run_parent,
    ResourceType.HITL_REQUEST: _hitl_parent,
    ResourceType.INTERACTIVE_ARTIFACT: _artifact_parent,
    ResourceType.BACKGROUND_JOB: _background_job_parent,
    ResourceType.BROWSER_BINDING: _browser_binding_parent,
    ResourceType.VFS_RUN: _vfs_run_parent,
    ResourceType.WORKFLOW_EXECUTION: _workflow_execution_parent,
    ResourceType.TASK_EXECUTION: _task_execution_parent,
    ResourceType.DEPLOYMENT_INVOCATION: _deployment_invocation_parent,
    ResourceType.KNOWLEDGE_BASE_FILE: _knowledge_base_file_parent,
    ResourceType.SKILL_REVISION: _skill_revision_parent,
}

AUTHORIZATION_ROOT_TYPES = frozenset({
    ResourceType.ORGANIZATION,
    ResourceType.GROUP,
    ResourceType.CHAT,
    ResourceType.WORKFLOW,
    ResourceType.TEMPLATE,
    ResourceType.TASK,
    ResourceType.DEPLOYMENT,
    ResourceType.STORAGE_ROOT,
    ResourceType.KNOWLEDGE_BASE,
    ResourceType.MCP_INSTALLATION,
    ResourceType.SKILL_INSTALLATION,
    ResourceType.LLM_CREDENTIAL,
    ResourceType.SERVICE_ACCOUNT,
})


async def resolve_authorization_root(
    session: AsyncSession,
    resource: ResourceRef,
) -> ResourceRef | None:
    if resource.type in AUTHORIZATION_ROOT_TYPES:
        return resource
    resolver = AUTHZ_PARENT_RESOLVERS.get(resource.type)
    return await resolver(session, resource) if resolver else None


async def collaboration_root_exists(session: AsyncSession, resource: ResourceRef) -> bool:
    """Reject deleted collaboration roots even while remote grants remain.

    The caller must bind the owner's RLS scope before this existence check;
    it does not replace authorization or read any resource payload.
    """
    from vibecanvas_api.storage.models import Workflow
    from vibecanvas_api.storage.models_tasks import Task
    from vibecanvas_api.storage.models_deployments import Deployment
    from vibecanvas_api.storage.models_skills import Skill
    from vibecanvas_api.storage.models_kb import KnowledgeBase

    roots = {
        ResourceType.WORKFLOW: (Workflow, Workflow.wf_id),
        ResourceType.TASK: (Task, Task.id),
        ResourceType.DEPLOYMENT: (Deployment, Deployment.id),
        ResourceType.SKILL_INSTALLATION: (Skill, Skill.skill_id),
        ResourceType.KNOWLEDGE_BASE: (KnowledgeBase, KnowledgeBase.id),
    }
    entry = roots.get(resource.type)
    if entry is None:
        return True
    model, key = entry
    try:
        identifier = resource.id if resource.type is ResourceType.WORKFLOW else uuid.UUID(resource.id)
    except ValueError:
        return False
    query = select(key).where(key == identifier)
    if hasattr(model, "deleted_at"):
        query = query.where(model.deleted_at.is_(None))
    return (await session.execute(query)).scalar_one_or_none() is not None
