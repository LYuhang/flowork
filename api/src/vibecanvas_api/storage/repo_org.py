"""Tenant-bound organization, group, and membership persistence."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.storage.models import User
from vibecanvas_api.security.identity_protection import decrypt_user_profile, profile_email_lookup_digest
from vibecanvas_api.storage.models_org import (
    Group,
    GroupMembership,
    Organization,
    OrgMembership,
)


MAX_GROUP_DEPTH = 8


class OrganizationRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_membership_changes(self) -> None:
        """Serialize role changes so concurrent owners cannot both leave."""
        organization_id = await self._current_tenant_id()
        await self.session.execute(
            select(Organization.tenant_id)
            .where(Organization.tenant_id == organization_id)
            .with_for_update()
        )

    async def add_registered_member(self, *, email: str, invited_by: uuid.UUID) -> OrgMembership:
        organization = await self.get_current()
        if organization is None or organization.kind != "business":
            raise ValueError("business_organization_required")
        users = list((await self.session.execute(select(User).where(
            User.profile_email_lookup_hash == profile_email_lookup_digest(email),
            User.status == "active",
        ).limit(2))).scalars())
        if len(users) != 1:
            raise ValueError("registered_user_not_found")
        existing = (await self.session.execute(select(OrgMembership).where(
            OrgMembership.tenant_id == organization.tenant_id,
            OrgMembership.user_id == users[0].user_id,
        ))).scalar_one_or_none()
        if existing is not None:
            raise ValueError("organization_membership_already_exists")
        membership = OrgMembership(tenant_id=organization.tenant_id,
            user_id=users[0].user_id, org_role="member", status="active",
            invited_by=invited_by, source="native")
        self.session.add(membership)
        await self.session.flush()
        return membership

    async def _current_tenant_id(self) -> uuid.UUID | None:
        """Read the server-bound organization scope for explicit predicates.

        Organization membership is global to a user, so relying on an implicit
        RLS predicate alone makes cardinality and owner-invariant checks fragile.
        Keep the tenant condition visible in every identity-sensitive query.
        """
        value = (
            await self.session.execute(
                text(
                    "SELECT NULLIF("
                    "current_setting('app.tenant_id', true), '')::uuid"
                )
            )
        ).scalar_one()
        return uuid.UUID(str(value)) if value else None

    async def get_current(self) -> Organization | None:
        organization_id = (
            await self.session.execute(
                text(
                    "SELECT NULLIF("
                    "current_setting('app.tenant_id', true), '')::uuid"
                )
            )
        ).scalar_one()
        return (
            await self.session.get(Organization, organization_id)
            if organization_id
            else None
        )

    async def list_members(self) -> list[dict]:
        rows = (
            await self.session.execute(
                select(OrgMembership, User)
                .join(User, User.user_id == OrgMembership.user_id)
                .order_by(
                    OrgMembership.created_at,
                    OrgMembership.membership_id,
                )
            )
        ).all()
        result = []
        for membership, user in rows:
            profile = await decrypt_user_profile(self.session, user)
            result.append({
                "membership_id": str(membership.membership_id),
                "user_id": str(membership.user_id),
                "email": profile.email,
                "display_name": profile.display_name,
                "role": membership.org_role,
                "status": membership.status,
                "source": membership.source,
                "directory_provider_id": (
                    str(membership.directory_provider_id)
                    if membership.directory_provider_id else None
                ),
                "created_at": membership.created_at,
                "updated_at": membership.updated_at,
            })
        return result

    async def get_member(self, user_id: uuid.UUID, *, include_revoked: bool = False) -> OrgMembership | None:
        tenant_id = await self._current_tenant_id()
        if tenant_id is None:
            return None
        return (
            await self.session.execute(
                select(OrgMembership).where(
                    OrgMembership.user_id == user_id,
                    OrgMembership.tenant_id == tenant_id,
                    True if include_revoked else OrgMembership.status != "revoked",
                )
            )
        ).scalar_one_or_none()

    async def get_member_projection(self, user_id: uuid.UUID) -> dict | None:
        tenant_id = await self._current_tenant_id()
        if tenant_id is None:
            return None
        row = (
            await self.session.execute(
                select(OrgMembership, User)
                .join(User, User.user_id == OrgMembership.user_id)
                .where(
                    OrgMembership.user_id == user_id,
                    OrgMembership.tenant_id == tenant_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        membership, user = row
        profile = await decrypt_user_profile(self.session, user)
        return {
            "membership_id": str(membership.membership_id),
            "user_id": str(membership.user_id),
            "email": profile.email,
            "display_name": profile.display_name,
            "role": membership.org_role,
            "status": membership.status,
            "source": membership.source,
            "directory_provider_id": (
                str(membership.directory_provider_id)
                if membership.directory_provider_id else None
            ),
            "created_at": membership.created_at,
            "updated_at": membership.updated_at,
        }

    async def get_self_summary(self, user_id: uuid.UUID) -> dict | None:
        """Return only the caller's membership and direct active groups."""
        membership = await self.get_member_projection(user_id)
        if membership is None:
            return None
        rows = (
            await self.session.execute(
                select(GroupMembership, Group)
                .join(
                    Group,
                    (Group.group_id == GroupMembership.group_id)
                    & (Group.tenant_id == GroupMembership.tenant_id),
                )
                .where(
                    GroupMembership.user_id == user_id,
                    GroupMembership.status != "revoked",
                    Group.status == "active",
                )
                .order_by(Group.name, Group.group_id)
            )
        ).all()
        return {
            "membership": membership,
            "groups": [
                {
                    "group_id": str(group.group_id),
                    "kind": group.kind,
                    "name": group.name,
                    "source": group.source,
                    "role": group_membership.group_role,
                    "status": group_membership.status,
                }
                for group_membership, group in rows
            ],
        }

    async def update_member(
        self,
        membership: OrgMembership,
        *,
        role: str,
        status: str,
    ) -> OrgMembership:
        """Update one membership while preserving an active organization owner."""
        await self.lock_membership_changes()
        await self.session.refresh(membership)
        if membership.source == "scim":
            raise ValueError("scim_managed_membership_read_only")
        removes_active_owner = (
            membership.org_role == "owner"
            and membership.status == "active"
            and (role != "owner" or status != "active")
        )
        if removes_active_owner:
            active_owner_count = int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(OrgMembership)
                        .where(
                            OrgMembership.tenant_id == membership.tenant_id,
                            OrgMembership.org_role == "owner",
                            OrgMembership.status == "active",
                        )
                    )
                ).scalar_one()
            )
            if active_owner_count <= 1:
                raise ValueError("organization_requires_active_owner")

        await self.session.execute(
            text(
                "UPDATE org_memberships "
                "SET org_role = :role, status = :status, updated_at = now() "
                "WHERE membership_id = :membership_id"
            ),
            {
                "role": role,
                "status": status,
                "membership_id": membership.membership_id,
            },
        )
        await self.session.flush()
        await self.session.refresh(membership)
        return membership


class GroupHierarchyError(ValueError):
    pass


class GroupRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_groups(
        self,
        *,
        include_archived: bool = False,
        authorized_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[Group]:
        """List the tenant rows already admitted by OpenFGA ListObjects.

        ``None`` is reserved for internal structural callers. HTTP collection
        routes always pass an explicit set (including the empty set), keeping
        relationship visibility and tenant/RLS selection as two independent
        mandatory gates.
        """
        if authorized_ids is not None and not authorized_ids:
            return []
        query = select(Group)
        if authorized_ids is not None:
            query = query.where(Group.group_id.in_(authorized_ids))
        if not include_archived:
            query = query.where(Group.status == "active")
        return list(
            (
                await self.session.execute(
                    query.order_by(Group.created_at, Group.group_id)
                )
            ).scalars()
        )

    async def get(self, group_id: uuid.UUID) -> Group | None:
        return await self.session.get(Group, group_id)

    async def _validated_parent(
        self,
        *,
        group_id: uuid.UUID | None,
        parent_group_id: uuid.UUID | None,
    ) -> Group | None:
        if parent_group_id is None:
            return None
        if group_id is not None and parent_group_id == group_id:
            raise GroupHierarchyError("group_cannot_parent_itself")
        parent = await self.get(parent_group_id)
        if parent is None or parent.status != "active":
            raise GroupHierarchyError("parent_group_not_found")

        current = parent
        depth = 1
        visited = {parent.group_id}
        while current.parent_group_id is not None:
            if group_id is not None and current.parent_group_id == group_id:
                raise GroupHierarchyError("group_cycle")
            if current.parent_group_id in visited:
                # Corrupt legacy data must fail closed instead of hanging.
                raise GroupHierarchyError("group_cycle")
            visited.add(current.parent_group_id)
            depth += 1
            if depth >= MAX_GROUP_DEPTH:
                raise GroupHierarchyError("group_depth_exceeded")
            current = await self.get(current.parent_group_id)
            if current is None:
                raise GroupHierarchyError("parent_group_not_found")
        return parent

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        created_by: uuid.UUID,
        name: str,
        kind: str,
        parent_group_id: uuid.UUID | None,
    ) -> Group:
        await self._validated_parent(
            group_id=None,
            parent_group_id=parent_group_id,
        )
        group = Group(
            tenant_id=organization_id,
            name=name,
            kind=kind,
            parent_group_id=parent_group_id,
            created_by=created_by,
            source="native",
            status="active",
        )
        self.session.add(group)
        await self.session.flush()
        return group

    async def update(
        self,
        group: Group,
        *,
        name: str | None = None,
        kind: str | None = None,
        parent_group_id: uuid.UUID | None | object = ...,
    ) -> Group:
        if group.source == "idp":
            raise GroupHierarchyError("idp_managed_group_read_only")
        if parent_group_id is not ...:
            await self._validated_parent(
                group_id=group.group_id,
                parent_group_id=parent_group_id,
            )
            group.parent_group_id = parent_group_id
        if name is not None:
            group.name = name
        if kind is not None:
            group.kind = kind
        await self.session.execute(
            text(
                "UPDATE groups SET updated_at = now() "
                "WHERE group_id = :group_id"
            ),
            {"group_id": group.group_id},
        )
        await self.session.flush()
        await self.session.refresh(group)
        return group

    async def archive(self, group: Group) -> None:
        if group.source == "idp":
            raise GroupHierarchyError("idp_managed_group_read_only")
        # Descendants would otherwise retain a parent that is hidden from the
        # active tree. Require callers to move/archive descendants explicitly.
        child = (
            await self.session.execute(
                select(Group.group_id)
                .where(
                    Group.parent_group_id == group.group_id,
                    Group.status == "active",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if child is not None:
            raise GroupHierarchyError("group_has_active_children")
        group.status = "archived"
        await self.session.execute(
            text(
                "UPDATE groups SET status = 'archived', updated_at = now() "
                "WHERE group_id = :group_id"
            ),
            {"group_id": group.group_id},
        )
        await self.session.flush()

    async def list_members(self, group_id: uuid.UUID) -> list[dict]:
        rows = (
            await self.session.execute(
                select(GroupMembership, User)
                .join(User, User.user_id == GroupMembership.user_id)
                .where(
                    GroupMembership.group_id == group_id,
                    GroupMembership.status != "revoked",
                )
                .order_by(
                    GroupMembership.created_at,
                    GroupMembership.membership_id,
                )
            )
        ).all()
        result = []
        for membership, user in rows:
            profile = await decrypt_user_profile(self.session, user)
            result.append({
                "membership_id": str(membership.membership_id),
                "user_id": str(membership.user_id),
                "email": profile.email,
                "display_name": profile.display_name,
                "role": membership.group_role,
                "status": membership.status,
                "created_at": membership.created_at,
                "updated_at": membership.updated_at,
            })
        return result

    async def set_member(
        self,
        *,
        group: Group,
        user_id: uuid.UUID,
        role: str,
        status: str,
    ) -> GroupMembership:
        if group.source == "idp":
            raise GroupHierarchyError("idp_managed_group_read_only")
        organization_membership = (
            await self.session.execute(
                select(OrgMembership).where(
                    OrgMembership.user_id == user_id,
                    OrgMembership.tenant_id == group.tenant_id,
                    OrgMembership.status == "active",
                )
            )
        ).scalar_one_or_none()
        if organization_membership is None:
            raise GroupHierarchyError("user_is_not_active_organization_member")
        membership = (
            await self.session.execute(
                select(GroupMembership).where(
                    GroupMembership.group_id == group.group_id,
                    GroupMembership.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            membership = GroupMembership(
                tenant_id=group.tenant_id,
                group_id=group.group_id,
                user_id=user_id,
                group_role=role,
                status=status,
            )
            self.session.add(membership)
        else:
            membership.group_role = role
            membership.status = status
            await self.session.execute(
                text(
                    "UPDATE group_memberships "
                    "SET group_role = :role, status = :status, updated_at = now() "
                    "WHERE membership_id = :membership_id"
                ),
                {
                    "role": role,
                    "status": status,
                    "membership_id": membership.membership_id,
                },
            )
        await self.session.flush()
        return membership

    async def get_membership(
        self,
        *,
        group_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> GroupMembership | None:
        return (
            await self.session.execute(
                select(GroupMembership).where(
                    GroupMembership.group_id == group_id,
                    GroupMembership.user_id == user_id,
                )
            )
        ).scalar_one_or_none()

    async def revoke_member(
        self,
        *,
        group_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> bool:
        group = await self.get(group_id)
        if group is not None and group.source == "idp":
            raise GroupHierarchyError("idp_managed_group_read_only")
        membership = await self.get_membership(
            group_id=group_id,
            user_id=user_id,
        )
        if membership is None:
            return False
        membership.status = "revoked"
        await self.session.execute(
            text(
                "UPDATE group_memberships "
                "SET status = 'revoked', updated_at = now() "
                "WHERE membership_id = :membership_id"
            ),
            {"membership_id": membership.membership_id},
        )
        await self.session.flush()
        return True

    async def effective_group_ids(self, user_id: uuid.UUID) -> Sequence[uuid.UUID]:
        """Return direct groups plus their active ancestors, bounded in SQL."""
        rows = (
            await self.session.execute(
                text(
                    """
                    WITH RECURSIVE effective(group_id, parent_group_id, depth) AS (
                        SELECT g.group_id, g.parent_group_id, 1
                        FROM group_memberships AS membership
                        JOIN groups AS g ON g.group_id = membership.group_id
                        WHERE membership.user_id = :user_id
                          AND membership.status = 'active'
                          AND g.status = 'active'
                        UNION
                        SELECT parent.group_id, parent.parent_group_id,
                               effective.depth + 1
                        FROM effective
                        JOIN groups AS parent
                          ON parent.group_id = effective.parent_group_id
                        WHERE effective.depth < :max_depth
                          AND parent.status = 'active'
                    )
                    SELECT DISTINCT group_id FROM effective
                    """
                ),
                {"user_id": user_id, "max_depth": MAX_GROUP_DEPTH},
            )
        ).scalars()
        return list(rows)


# Temporary import aliases are intentionally omitted: new code must use the
# generic Group vocabulary instead of extending the removed department model.
OrgRepo = OrganizationRepo
