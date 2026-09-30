"""Validate deployment resource dependencies before a candidate can serve traffic."""
from __future__ import annotations

import tempfile
from uuid import UUID

from sqlalchemy import text

from vibecanvas_api.authorization.dependencies import authz_service_for_session
from vibecanvas_api.authorization.mutations import AuthzMutationCoordinator
from vibecanvas_api.authorization.projection import apply_committed_structural_mutations
from vibecanvas_api.services.service_account_resources import delegate_new_resources
from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
from vibecanvas_api.authorization.types import (
    Action, AuthzRequestContext, ConsistencyPreference, PrincipalRef,
    PrincipalType, ResourceRef, ResourceType,
)
from vibecanvas_api.services.workflow_resources import (
    collect_subagent_resources, materialize_workflow_skills, resolve_workflow_resources,
)
from vibecanvas_api.storage.db import short_session_scope


async def validate_deployment_resources(*, tenant_id: str, revision_id: str,
                                        spec: dict, workflow: dict, sandbox_session=None) -> None:
    """Check persisted identity, resource contracts and package bytes without tools.

    No invocation is fabricated and no business tool or model is called. The
    invocation still resolves its own latest snapshot when it is accepted.
    """
    if not collect_subagent_resources(workflow):
        return
    client = openfga_client_from_config()
    try:
        async with short_session_scope(tenant_id=tenant_id) as session:
            service, principal, context = await _authorize_revision(session=session, client=client,
                tenant_id=tenant_id, revision_id=revision_id, spec=spec)
            coordinator = AuthzMutationCoordinator(client=client, organization_id=tenant_id)
            mutations = await delegate_new_resources(session=session, coordinator=coordinator,
                tenant_id=tenant_id, service_account_id=spec['service_account_id'],
                created_by=spec['user_id'], workflow=workflow)
        await apply_committed_structural_mutations(coordinator, mutations)
        # Revalidate after the grant transaction commits and its projection is
        # applied. A concurrent account disable/revocation cannot be bypassed.
        async with short_session_scope(tenant_id=tenant_id) as session:
            service, principal, context = await _authorize_revision(session=session, client=client,
                tenant_id=tenant_id, revision_id=revision_id, spec=spec)
            snapshot = await resolve_workflow_resources(session=session, workflow=workflow,
                service=service, principal=principal, context=context)
            if sandbox_session is None:
                with tempfile.TemporaryDirectory(prefix='flowork-deployment-skills-') as root:
                    await materialize_workflow_skills(session=session, root=root, snapshot=snapshot)
            else:
                snapshot['execution'] = {
                    'execution_resource_type': 'deployment_preparation',
                    'organization_id': tenant_id, 'revision_id': revision_id, 'spec': spec,
                    'principal_generation': context.authz_generation,
                }
                snapshot['lease_id'] = 'deployment-preparation'
        if sandbox_session is not None:
            await sandbox_session.prepare_workflow_skills(snapshot)
    finally:
        await client.close()


async def _authorize_revision(*, session, client, tenant_id, revision_id, spec):
    row = (await session.execute(text("""
        SELECT r.spec, d.id AS deployment_id, a.generation
        FROM deployment_runtime_revisions r
        JOIN deployments d ON d.id=r.deployment_id
        JOIN service_accounts a ON a.service_account_id=d.service_account_id
        WHERE r.id=:revision AND r.tenant_id=:tenant
          AND r.state IN ('preparing','active','draining')
          AND d.deleted_at IS NULL AND d.enabled
          AND a.service_account_id=:account AND a.tenant_id=:tenant
          AND a.status='active' AND a.created_by=:creator
          AND a.owner_resource_type='deployment' AND a.owner_resource_id=d.id::text
                  FOR UPDATE OF a
    """), {'revision': UUID(revision_id), 'tenant': UUID(tenant_id),
           'account': UUID(spec['service_account_id']), 'creator': UUID(spec['user_id'])}
    )).mappings().one_or_none()
    if row is None or row['spec'] != spec:
        raise PermissionError('deployment_resource_identity_unavailable')
    service = authz_service_for_session(session=session,
        organization_id=tenant_id, openfga_client=client)
    principal = PrincipalRef(PrincipalType.SERVICE_ACCOUNT, spec['service_account_id'])
    context = AuthzRequestContext(active_organization_id=tenant_id,
        authz_generation=row['generation'],
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    for kind, identifier in ((ResourceType.DEPLOYMENT, str(row['deployment_id'])),
                             (ResourceType.WORKFLOW, spec['wf_id'])):
        decision = await service.check(principal, Action.EXECUTE,
            ResourceRef(kind, identifier, tenant_id), context)
        if not decision.allowed:
            raise PermissionError('deployment_resource_execution_access_revoked')
    return service, principal, context


async def authorize_prepared_skills(request, claims, *, resolve):
    """Recheck a private preparation lease; never usable as an execution token."""
    from types import SimpleNamespace
    try:
        async with short_session_scope(tenant_id=claims['organization_id']) as session:
            service, principal, context = await _authorize_revision(session=session,
                client=request.app.state.openfga_client, tenant_id=claims['organization_id'],
                revision_id=claims['revision_id'], spec=claims['spec'])
            if context.authz_generation != claims['principal_generation']:
                return []
            return await resolve(session=session, service=service, principal=principal,
                authz_context=context, capability=SimpleNamespace(**claims))
    except PermissionError:
        return []
