"""Live Agent identity and Chat execution context shared by CLI and render tools."""

from __future__ import annotations

from vibecanvas_api.agents.tool_runtime import AgentContext
from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.auth.live_identity import LiveIdentityError, resolve_live_authorization_identity
from vibecanvas_api.authorization.dependencies import authz_service_for_session, scope_authz_service
from vibecanvas_api.authorization.openfga_client import OpenFgaUnavailableError
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.services.agent_resources.capability import AgentCapability
from vibecanvas_api.services.chat_workspace import (
    project_workspace_scope_id,
)
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.sync_repo import SyncWorkflowRepo
from vibecanvas_api.storage.vfs_store import PostgresVfsStore

_OPENFGA_CLIENT = None


def set_authorization_client(client) -> None:
    """Publish the API lifespan's shared authorization client."""
    global _OPENFGA_CLIENT
    _OPENFGA_CLIENT = client


async def resolve_context(capability: AgentCapability) -> AgentContext:
    """Rebuild ephemeral tool context from durable backend state."""
    identity = await resolve_identity(capability)
    async with session_scope(tenant_id=capability.tenant_id, user_id=capability.user_id) as session:
        service = authz_service_for_session(
            session=session,
            organization_id=capability.organization_id,
            openfga_client=_OPENFGA_CLIENT,
        )
        service = scope_authz_service(
            service,
            session=session,
            auth=identity,
            audit_uses=True,
        )
        try:
            chat_decision = await service.check(
                PrincipalRef(PrincipalType.USER, capability.user_id),
                Action.EXECUTE,
                ResourceRef(
                    ResourceType.CHAT,
                    capability.chat_id,
                    capability.organization_id,
                ),
                AuthzRequestContext(
                    active_organization_id=capability.organization_id,
                    request_id=f"agent-resource:{capability.turn_id}",
                    session_id=capability.session_id,
                    session_generation=capability.session_generation,
                    membership_id=identity.membership_id,
                    membership_role=identity.membership_role,
                    membership_status=identity.membership_status,
                    authentication_strength=identity.authentication_strength,
                    consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
                ),
            )
        except OpenFgaUnavailableError as exc:
            raise PermissionError(
                "Agent resource authorization is temporarily unavailable"
            ) from exc
        if not chat_decision.allowed:
            raise PermissionError("Agent resource Chat access has been revoked")
        run = await AgentRunsRepo(session).get_for_chat(
            capability.chat_id,
            capability.turn_id,
            creator_user_id=capability.user_id,
        )
        if run is None or run.status != "running":
            raise PermissionError("Agent resource capability is not bound to an active run")
        binding = await ChatRepo(
            session, capability.user_id
        ).get_platform_context_binding(capability.chat_id)
        if binding is None:
            raise PermissionError("Agent resource capability is not bound to an active Chat")
        if binding.get("runtime_session_id") != capability.runtime_session_id:
            raise PermissionError(
                "Agent resource capability Runtime binding is stale"
            )
        expected_workspace_scope_id = project_workspace_scope_id(binding["project_id"], workflow_id=binding.get("workflow_id"))
        if expected_workspace_scope_id != capability.workspace_scope_id:
            raise PermissionError(
                "Agent resource capability workspace does not match its Chat"
            )

    workflow_repo = SyncWorkflowRepo(capability.user_id)

    return AgentContext(
        # Resource commands resolve explicit targets after live authorization.
        # The Chat context itself does not select or preload any workflow.
        workflow={},
        repo=workflow_repo,
        vfs=PostgresVfsStore(),
        username=capability.user_id,
        wf_id=capability.workspace_scope_id,
        tenant_id=capability.tenant_id,
        chat_id=capability.chat_id,
        turn_id=capability.turn_id,
        authorization_client=_OPENFGA_CLIENT,
        authorization_session_id=capability.session_id,
        authorization_membership_id=identity.membership_id,
        authorization_membership_role=identity.membership_role,
        authorization_membership_status=identity.membership_status,
        authorization_session_generation=capability.session_generation,
        authorization_generation=capability.authorization_generation,
        authorization_authentication_strength=identity.authentication_strength,
        authorization_session_audience=identity.session_audience,
        authorization_privileged_access_request_id=(
            identity.privileged_access_request_id
        ),
        authorization_privileged_resource_type=identity.privileged_resource_type,
        authorization_privileged_resource_id=identity.privileged_resource_id,
        authorization_privileged_actions=tuple(identity.privileged_actions),
        authorization_privileged_expires_at=identity.privileged_expires_at,
        runtime_session_id=capability.runtime_session_id,
        surface="chat",
        approval_mode=capability.approval_mode,
        runtime_location="agent_host",
    )


async def resolve_identity(
    capability: AgentCapability,
) -> AuthContext:
    """Fence a capability to the still-live browser Session and membership."""
    from vibecanvas_api.services.agent_runtime.model_capability import (
        authorization_model_generation,
    )

    expected_generation = authorization_model_generation(
        model_id=config.openfga_authorization_model_id,
    )
    if capability.authorization_generation != expected_generation:
        raise PermissionError("Agent resource authorization generation is stale")

    async with session_scope() as identity_session:
        try:
            return await resolve_live_authorization_identity(
                identity_session,
                session_id=capability.session_id,
                user_id=capability.user_id,
                organization_id=capability.organization_id,
                session_generation=capability.session_generation,
                membership_id=capability.membership_id,
            )
        except LiveIdentityError as exc:
            raise PermissionError(
                "Agent resource browser identity has been revoked"
            ) from exc
