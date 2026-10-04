"""Recipient-bound sharing locators. These rows never grant access.

The maintenance query returns only root IDs/owners and ordering timestamps.
Callers must perform current OpenFGA checks before reading resource content.
Membership is evaluated live so no recipient expansion needs synchronizing.
"""
from dataclasses import dataclass
from datetime import datetime
import uuid

from sqlalchemy import text
from .sync_session import short_admin_connection


@dataclass(frozen=True)
class SharedRoot:
    owner_tenant_id: uuid.UUID
    resource_type: str
    resource_id: str
    last_projection_update: datetime


async def shared_resource_roots(user_id, *, active_organization_id, resource_type=None, resource_id=None,
                                execution_id=None, limit=None, offset=0):
    params = {'user': uuid.UUID(str(user_id)), 'kind': resource_type, 'resource': resource_id,
              'execution': uuid.UUID(str(execution_id)) if execution_id else None,
              'limit': limit, 'offset': offset, 'workspace': uuid.UUID(str(active_organization_id))}
    async with short_admin_connection() as connection:
        rows = (await connection.execute(text('''
            WITH RECURSIVE memberships AS (
                SELECT m.tenant_id, m.org_role FROM org_memberships m
                JOIN users u ON u.user_id=m.user_id AND u.status='active'
                WHERE m.user_id=:user AND m.status='active'
            ), direct_groups AS (
                SELECT g.group_id, g.tenant_id, g.parent_group_id FROM groups g
                JOIN group_memberships m ON m.group_id=g.group_id AND m.tenant_id=g.tenant_id
                JOIN memberships o ON o.tenant_id=g.tenant_id
                WHERE m.user_id=:user AND m.status='active' AND g.status='active'
            ), member_groups AS (
                SELECT * FROM direct_groups
                UNION
                SELECT g.group_id, g.tenant_id, g.parent_group_id FROM groups g
                JOIN member_groups child ON child.parent_group_id=g.group_id AND child.tenant_id=g.tenant_id
                WHERE g.status='active'
            ), candidates AS (
                SELECT owner_tenant_id, resource_type, resource_id, updated_at
                FROM shared_resource_projections WHERE recipient_user_id=:user
                UNION ALL
                SELECT m.tenant_id, m.object_type, m.object_id, COALESCE(m.applied_at, m.requested_at)
                FROM authz_mutations m JOIN authz_edge_revisions e
                  ON e.tenant_id=m.tenant_id AND e.object_type=m.object_type AND e.object_id=m.object_id
                 AND e.relation=m.relation AND e.subject_type=m.subject_type AND e.subject_id=m.subject_id
                 AND e.subject_relation=COALESCE(m.subject_relation,'') AND e.current_revision=m.edge_revision
                WHERE m.kind='direct_binding' AND m.status='applied' AND m.desired_state='present'
                  AND m.object_type IN ('workflow','task','deployment','skill_installation','knowledge_base')
                  AND (
                    (m.subject_type='organization' AND m.subject_relation='member' AND EXISTS (
                        SELECT 1 FROM memberships o WHERE o.tenant_id::text=m.subject_id
                        AND o.org_role IN ('owner','admin','member')))
                    OR (m.subject_type='group' AND m.subject_relation='direct_member' AND EXISTS (
                        SELECT 1 FROM direct_groups g WHERE g.group_id::text=m.subject_id))
                    OR (m.subject_type='group' AND m.subject_relation='member' AND EXISTS (
                        SELECT 1 FROM member_groups g WHERE g.group_id::text=m.subject_id))
                  )
            )
            SELECT owner_tenant_id, resource_type, resource_id, MAX(c.updated_at) AS last_projection_update
            FROM candidates c
            JOIN organizations owner ON owner.tenant_id=c.owner_tenant_id
            JOIN organizations workspace ON workspace.tenant_id=:workspace
            WHERE (owner.tenant_id=workspace.tenant_id OR
                   (owner.kind='personal' AND workspace.kind='personal'))
              AND EXISTS (SELECT 1 FROM memberships m WHERE m.tenant_id=workspace.tenant_id)
              AND (CAST(:kind AS text) IS NULL OR c.resource_type=:kind)
              AND (CAST(:resource AS text) IS NULL OR c.resource_id=:resource)
              AND (CAST(:execution AS uuid) IS NULL OR EXISTS (
                  SELECT 1 FROM workflow_execution_runs r WHERE r.id=:execution
                    AND r.tenant_id=c.owner_tenant_id AND r.source_type=c.resource_type AND r.source_id=c.resource_id))
            GROUP BY owner_tenant_id, resource_type, resource_id
            ORDER BY last_projection_update DESC, resource_type, resource_id, owner_tenant_id
            LIMIT :limit OFFSET :offset
        '''), params)).mappings().all()
    return [SharedRoot(**row) for row in rows]


async def allows_personal_cross_workspace_share(*, user_id: str, active_organization_id: str,
                                                owner_organization_id: str) -> bool:
    """Check only identity metadata; relationship authorization is still required.

    Business shares never cross workspace boundaries, including direct user
    grants. Read metadata independently of an admitted resource's RLS scope.
    Missing organizations or inactive membership fail closed.
    """
    async with short_admin_connection() as connection:
        return bool((await connection.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM organizations owner, organizations workspace
                JOIN org_memberships m ON m.tenant_id=workspace.tenant_id
                JOIN users u ON u.user_id=m.user_id
                WHERE owner.tenant_id=:owner AND workspace.tenant_id=:workspace
                  AND owner.kind='personal' AND workspace.kind='personal'
                  AND m.user_id=:user AND m.status='active' AND u.status='active'
            )
        """), {'owner': uuid.UUID(owner_organization_id),
               'workspace': uuid.UUID(active_organization_id), 'user': uuid.UUID(user_id)})).scalar_one())
