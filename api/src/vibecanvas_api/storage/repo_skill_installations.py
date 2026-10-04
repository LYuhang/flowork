"""Actor-private installation choices; callers must separately authorize Skill use.

An installation is not a permission grant or a copy of a shared Skill package.
RLS uses the actor rather than the currently selected organization, so changing
workspaces cannot change another user's installation or hide one's own choice.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class SkillInstallationsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def installed_ids(self, user_id: uuid.UUID) -> set[str]:
        rows = await self.session.execute(text(
            "SELECT skill_id FROM user_skill_installations WHERE user_id=:user_id"
        ), {"user_id": user_id})
        return {str(identifier) for identifier in rows.scalars()}

    async def install(self, user_id: uuid.UUID, skill_id: uuid.UUID) -> None:
        await self.session.execute(text(
            "INSERT INTO user_skill_installations (user_id, skill_id) "
            "VALUES (:user_id, :skill_id) ON CONFLICT (user_id, skill_id) "
            "DO UPDATE SET installed_at=clock_timestamp()"
        ), {"user_id": user_id, "skill_id": skill_id})

    async def uninstall(self, user_id: uuid.UUID, skill_id: uuid.UUID) -> None:
        await self.session.execute(text(
            "DELETE FROM user_skill_installations WHERE user_id=:user_id AND skill_id=:skill_id"
        ), {"user_id": user_id, "skill_id": skill_id})
