"""Live DB membership discovery; locators do not replace authorization."""
import uuid
import pytest
from sqlalchemy import text
from vibecanvas_api.authorization.mutations import AuthzMutationCoordinator
from vibecanvas_api.authorization.types import (
    PrincipalRef, PrincipalType, RelationshipBinding, RelationshipSubject,
    RelationshipSubjectType, ResourceRef, ResourceType,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.shared_resource_locator import shared_resource_roots
from tests.test_authz_projection_integration import _seed, _TupleStore


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['company', 'department', 'direct_department'])
async def test_membership_discovery_and_revoke_do_not_need_user_projection(pg_engine, source):
    org, user, parent, child, workflow = await _seed()
    store = _TupleStore()
    coordinator = AuthzMutationCoordinator(client=store, organization_id=org)
    principal = PrincipalRef(PrincipalType.USER, user)
    subject = (RelationshipSubject(RelationshipSubjectType.ORGANIZATION, org, 'member') if source == 'company'
               else RelationshipSubject(RelationshipSubjectType.GROUP, parent,
                   'direct_member' if source == 'direct_department' else 'member'))
    binding = RelationshipBinding(subject, 'editor', ResourceRef(ResourceType.WORKFLOW, workflow, org))
    async def grant(binding, present):
        await coordinator.request_binding(actor=principal, binding=binding, desired_present=present,
                                          idempotency_key='locator-' + uuid.uuid4().hex)
    await grant(binding, True)
    rows = await shared_resource_roots(user, active_organization_id=org, resource_type='workflow', resource_id=workflow)
    if source == 'direct_department':
        assert rows == []  # Child membership does not imply direct parent membership.
        return
    assert [(str(row.owner_tenant_id), row.resource_id) for row in rows] == [(org, workflow)]
    async with session_scope(tenant_id=org) as session:
        assert (await session.execute(text('SELECT count(*) FROM shared_resource_projections WHERE resource_id=:id'),
                                      {'id': workflow})).scalar_one() == 0
        table = 'org_memberships' if source == 'company' else 'group_memberships'
        await session.execute(text(f"UPDATE {table} SET status='suspended' WHERE user_id=:user"), {'user': uuid.UUID(user)})
    assert await shared_resource_roots(user, active_organization_id=org, resource_id=workflow) == []
    async with session_scope(tenant_id=org) as session:
        await session.execute(text(f"UPDATE {table} SET status='active' WHERE user_id=:user"), {'user': uuid.UUID(user)})
    assert len(await shared_resource_roots(user, active_organization_id=org, resource_id=workflow)) == 1
    direct = RelationshipBinding(RelationshipSubject(RelationshipSubjectType.USER, user), 'editor', binding.resource)
    await grant(direct, True)
    assert len(await shared_resource_roots(user, active_organization_id=org, resource_id=workflow)) == 1
    await grant(binding, False)
    assert len(await shared_resource_roots(user, active_organization_id=org, resource_id=workflow)) == 1
    await grant(direct, False)
    assert await shared_resource_roots(user, active_organization_id=org, resource_id=workflow) == []
