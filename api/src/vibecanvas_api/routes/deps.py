"""Shared FastAPI dependencies for route modules.

Every business-route repository is built from the authenticated
`current_user` context on a tenant-bound DB session (`tenant_db`), so
Postgres RLS isolates the request to its tenant. The session lifecycle
(commit on success / rollback on error) is owned by FastAPI dependency
teardown via `tenant_db` → `session_scope`, not the route.
"""
from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import AuthContext, current_user, tenant_db
from ..storage.chat_repo import ChatRepo
from ..storage.chat_project_repo import ChatProjectRepo
from ..storage.agent_runtime_repo import AgentRuntimeRepo
from ..storage.execution_repo import ExecutionRepo
from ..storage.workflow_repo import WorkflowRepo
from ..storage.db import session_scope


async def get_workflow_repo(
    session: AsyncSession = Depends(tenant_db),
    ctx: AuthContext = Depends(current_user),
) -> WorkflowRepo:
    """Per-request, tenant-bound WorkflowRepo. RLS isolates by tenant;
    `creator_user_id` rows are stamped with the authenticated user."""
    return WorkflowRepo(session, ctx.user_id)


async def get_execution_repo(
    ctx: AuthContext = Depends(current_user),
):
    """Execution state belongs to the actor, even while reading a shared Workflow."""
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.user_id) as session:
        yield ExecutionRepo(session, ctx.user_id)


async def get_chat_repo(
    session: AsyncSession = Depends(tenant_db),
    ctx: AuthContext = Depends(current_user),
) -> ChatRepo:
    """Per-request, tenant-bound ChatRepo. `register_session` runs in the
    request handler before streaming starts. Runtime-native state is separate;
    completed product messages are projected into ChatRepo by the durable Turn
    writer."""
    return ChatRepo(session, ctx.user_id)


async def get_chat_project_repo(
    session: AsyncSession = Depends(tenant_db),
    ctx: AuthContext = Depends(current_user),
) -> ChatProjectRepo:
    """Tenant- and user-bound Project repository."""
    return ChatProjectRepo(session, ctx.user_id)


async def get_agent_runtime_repo(
    session: AsyncSession = Depends(tenant_db),
    ctx: AuthContext = Depends(current_user),
) -> AgentRuntimeRepo:
    """User defaults and immutable per-Chat Agent Runtime bindings."""
    return AgentRuntimeRepo(session, ctx.user_id)


async def get_agent_runs_repo(
    session: AsyncSession = Depends(tenant_db),
):
    """Per-request durable Agent Turn control-plane repository."""
    from ..storage.agent_runs_repo import AgentRunsRepo
    return AgentRunsRepo(session)


async def get_hitl_repo(
    session: AsyncSession = Depends(tenant_db),
):
    """Per-request durable HITL / interactive-artifact repository."""
    from ..storage.hitl_repo import HitlRepo
    return HitlRepo(session)
