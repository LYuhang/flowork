"""Workspace isolation applies even with a valid direct grant/admission."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from tests.test_repo_org import _seed_tenant_user_and_backfill
from vibecanvas_api.authorization.openfga import OpenFgaAuthzService
from vibecanvas_api.authorization.types import (
    Action, AuthorizationCheck, AuthzRequestContext, PrincipalRef, PrincipalType,
    ResourceRef, ResourceType,
)
from vibecanvas_api.storage.db import session_scope


@pytest.mark.asyncio
@pytest.mark.parametrize('owner_kind,workspace_kind,allowed', [
    ('personal', 'personal', True), ('business', 'personal', False),
    ('personal', 'business', False), ('business', 'business', False),
])
@pytest.mark.parametrize('kind', [ResourceType.WORKFLOW, ResourceType.TASK,
    ResourceType.DEPLOYMENT, ResourceType.SKILL_INSTALLATION, ResourceType.KNOWLEDGE_BASE])
async def test_direct_grant_cannot_cross_business_boundary(
        pg_engine, monkeypatch, owner_kind, workspace_kind, allowed, kind):
    owner, _ = await _seed_tenant_user_and_backfill(pg_engine)
    workspace, user = await _seed_tenant_user_and_backfill(pg_engine)
    async with pg_engine.begin() as conn:
        for organization, value in [(owner, owner_kind), (workspace, workspace_kind)]:
            await conn.execute(text('UPDATE organizations SET kind=:kind WHERE tenant_id=:id'),
                               {'id': organization, 'kind': value})
    root = ResourceRef(kind, 'shared-root', str(owner))
    context = AuthzRequestContext(active_organization_id=str(workspace), membership_status='active',
        membership_role='owner', admitted_resource_organization_id=str(owner),
        admitted_resource_type=kind.value, admitted_resource_id=root.id)
    principal = PrincipalRef(PrincipalType.USER, str(user))
    client = AsyncMock()
    async with session_scope(tenant_id=str(owner), user_id=str(user)) as session:
        service = OpenFgaAuthzService(session, client)
        monkeypatch.setattr(service, 'resolve_parent', AsyncMock(return_value=root))
        # All graph relationships grant access: the SQL workspace boundary must
        # still win, including in batch capability checks used by list pages.
        async def checks(items, **kwargs):
            return tuple(True for _ in items)
        client.batch_check.side_effect = checks
        decision = await service.check(principal, Action.VIEW_METADATA, root, context)
        assert decision.allowed is allowed
        if not allowed:
            assert decision.reason_code == 'workspace_boundary'
            client.batch_check.assert_not_called()
        decisions = await service.batch_check((AuthorizationCheck(principal, Action.VIEW_METADATA, root, context),))
        assert decisions[0].allowed is allowed
        # Switching back into the resource's own company restores the ordinary
        # relationship check, rather than deleting the user's sharing grant.
        if owner_kind == 'business':
            inside = replace(context, active_organization_id=str(owner))
            assert (await service.check(principal, Action.VIEW_METADATA, root, inside)).allowed
