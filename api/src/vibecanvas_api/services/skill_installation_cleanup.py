"""Invalidate personal opt-ins after an authorization edge is removed.

Only identifiers and lifecycle eligibility are read across tenants.
Company packages require active company membership even when retained department
tuples still yield a positive raw FGA result. The FGA result is used to remove
installation choices, never to grant access or read package contents.
"""
from sqlalchemy import text

from vibecanvas_api.authorization.types import ConsistencyPreference
from vibecanvas_api.storage.sync_session import short_admin_connection


async def discard_inaccessible_installations(client, *, skill_id: str | None = None, user_id: str | None = None):
    async with short_admin_connection() as connection:
        rows = (await connection.execute(text("""
            SELECT i.user_id, i.skill_id, i.installed_at, u.status,
                   (s.deleted_at IS NULL AND (o.kind = 'personal' OR EXISTS (
                       SELECT 1 FROM org_memberships m WHERE m.tenant_id=s.tenant_id
                         AND m.user_id=i.user_id AND m.status='active'
                   ))) AS eligible
            FROM user_skill_installations i JOIN users u ON u.user_id=i.user_id
            JOIN skills s ON s.skill_id=i.skill_id
            JOIN organizations o ON o.tenant_id=s.tenant_id
            WHERE (CAST(:skill AS text) IS NULL OR i.skill_id::text=:skill)
              AND (CAST(:user AS text) IS NULL OR i.user_id::text=:user)
        """), {"skill": skill_id, "user": user_id})).mappings().all()
    retained = set()
    for start in range(0, len(rows), 100):
        batch = rows[start:start + 100]
        allowed = await client.batch_check(tuple(
            (f'user:{row["user_id"]}', 'can_use', f'skill_installation:{row["skill_id"]}')
            for row in batch
        ), consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        if len(allowed) != len(batch):
            raise RuntimeError('Skill installation authorization result is incomplete')
        rejected = [row for row, access in zip(batch, allowed, strict=True)
                    if not access or row['status'] != 'active' or not row['eligible']]
        retained.update((str(row['user_id']), str(row['skill_id']))
                        for row, access in zip(batch, allowed, strict=True)
                        if access and row['status'] == 'active' and row['eligible'])
        if rejected:
            async with short_admin_connection() as connection:
                async with connection.begin():
                    for row in rejected:
                        # Do not erase a subsequent explicit reinstallation.
                        await connection.execute(text("""
                            DELETE FROM user_skill_installations
                            WHERE user_id=:user_id AND skill_id=:skill_id AND installed_at=:installed_at
                        """), {key: row[key] for key in ('user_id', 'skill_id', 'installed_at')})
    return retained
