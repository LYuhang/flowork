"""Locate explicitly shared inventory roots; never treat discovery as authority."""
from dataclasses import replace

from vibecanvas_api.authorization.types import Action, ConsistencyPreference, ResourceRef
from vibecanvas_api.storage.shared_resource_locator import shared_resource_roots


def inventory_context(context, owner: str, kind: str, identifier: str):
    if owner == context.active_organization_id:
        return context
    return replace(context, admitted_resource_organization_id=owner,
        admitted_resource_type=kind, admitted_resource_id=identifier,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY)


async def inventory_groups(*, service, principal, context, resource_type, local_ids):
    """Return deduplicated IDs by owner, checking every foreign root live."""
    groups = {context.active_organization_id: set(local_ids)}
    for root in await shared_resource_roots(principal.id, active_organization_id=context.active_organization_id, resource_type=resource_type.value):
        owner = str(root.owner_tenant_id)
        if owner == context.active_organization_id:
            continue
        decision = await service.check(principal, Action.VIEW_METADATA,
            ResourceRef(resource_type, root.resource_id, owner),
            inventory_context(context, owner, resource_type.value, root.resource_id))
        if decision.allowed:
            groups.setdefault(owner, set()).add(root.resource_id)
    return {owner: sorted(ids) for owner, ids in groups.items()}
