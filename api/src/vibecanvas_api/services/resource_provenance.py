"""Canonical, recipient-safe provenance for product resource responses."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.schemas.access import (
    ResourceOrigin,
    ResourcePartyOut,
    ResourceProvenanceOut,
)
from vibecanvas_api.security.identity_protection import decrypt_user_profile
from vibecanvas_api.storage.models import User
from vibecanvas_api.storage.models_org import Organization


class ResourceProvenanceBuilder:
    """Build provenance from authoritative owner data under the current RLS.

    IDs and tenant keys deliberately stay out of the projection. The builder
    caches the current organization and decrypted user labels so list routes
    do not repeat identity work for every card.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._organization: Organization | None = None
        self._organization_loaded = False
        self._parties: dict[uuid.UUID, ResourcePartyOut | None] = {}

    async def _current_organization(self) -> Organization:
        if self._organization_loaded:
            if self._organization is None:  # pragma: no cover - invariant
                raise RuntimeError("resource organization is unavailable")
            return self._organization
        value = (
            await self._session.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            )
        ).scalar_one()
        if not value:
            raise RuntimeError("resource provenance requires a tenant context")
        self._organization = await self._session.get(
            Organization,
            uuid.UUID(str(value)),
        )
        self._organization_loaded = True
        if self._organization is None:
            raise RuntimeError("resource organization is unavailable")
        return self._organization

    async def _user_party(
        self,
        user_id: uuid.UUID | str | None,
    ) -> ResourcePartyOut | None:
        if user_id is None or not str(user_id):
            return None
        typed_id = uuid.UUID(str(user_id))
        if typed_id in self._parties:
            return self._parties[typed_id]
        user = await self._session.get(User, typed_id)
        if user is None:
            self._parties[typed_id] = None
            return None
        profile = await decrypt_user_profile(self._session, user)
        party = ResourcePartyOut(
            type="user",
            display_name=profile.display_name.strip() or "Account",
        )
        self._parties[typed_id] = party
        return party

    async def build(
        self,
        *,
        creator_user_id: uuid.UUID | str | None,
        origin_type: ResourceOrigin = "created",
    ) -> ResourceProvenanceOut:
        organization = await self._current_organization()
        creator = await self._user_party(creator_user_id)
        if organization.kind == "personal":
            owner = (
                await self._user_party(organization.created_by)
                or creator
                or ResourcePartyOut(
                    type="user",
                    display_name="Personal account",
                )
            )
            ownership_scope = "personal"
        else:
            owner = ResourcePartyOut(
                type="organization",
                display_name=organization.name,
            )
            ownership_scope = "organization"
        return ResourceProvenanceOut(
            ownership_scope=ownership_scope,
            origin_type=origin_type,
            owner=owner,
            created_by=creator,
        )


def platform_provenance(
    *,
    origin_type: ResourceOrigin = "system",
    display_name: str = "Flowork",
) -> ResourceProvenanceOut:
    """Return provenance for a platform-owned catalog or built-in resource."""
    return ResourceProvenanceOut(
        ownership_scope="platform",
        origin_type=origin_type,
        owner=ResourcePartyOut(type="platform", display_name=display_name),
        created_by=None,
    )
