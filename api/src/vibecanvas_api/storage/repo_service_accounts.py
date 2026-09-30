"""Persistence boundary for durable Service Account execution leases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.audit import actions as audit_actions
from vibecanvas_api.audit.service import record_audit
from vibecanvas_api.storage.models_service_accounts import (
    ServiceAccount,
    ServiceAccountCredential,
)


@dataclass(frozen=True, slots=True)
class ServiceAccountLease:
    service_account_id: uuid.UUID
    tenant_id: uuid.UUID
    generation: int
    created_by: uuid.UUID
    kind: str
    owner_resource_type: str
    owner_resource_id: str


class ServiceAccountsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_for_owner(
        self,
        *,
        service_account_id: uuid.UUID,
        tenant_id: uuid.UUID,
        name: str,
        kind: str,
        owner_resource_type: str,
        owner_resource_id: str,
        created_by: uuid.UUID,
        status: str = "active",
    ) -> ServiceAccount:
        row = ServiceAccount(
            service_account_id=service_account_id,
            tenant_id=tenant_id,
            name=name,
            kind=kind,
            owner_resource_type=owner_resource_type,
            owner_resource_id=owner_resource_id,
            created_by=created_by,
            status=status,
            disabled_at=(
                datetime.now(timezone.utc) if status != "active" else None
            ),
        )
        self.session.add(row)
        await record_audit(
            self.session,
            action=audit_actions.SERVICE_ACCOUNT_CREATE,
            actor_user_id=created_by,
            actor_email=None,
            target_type=audit_actions.TARGET_SERVICE_ACCOUNT,
            target_id=str(service_account_id),
            target_name=name,
            outcome="success",
            meta={
                "kind": kind,
                "owner_resource_type": owner_resource_type,
                "owner_resource_id": owner_resource_id,
                "status": status,
            },
        )
        await self.session.flush()
        return row

    async def get(self, service_account_id: uuid.UUID) -> ServiceAccount | None:
        return await self.session.get(ServiceAccount, service_account_id)

    async def get_for_owner(
        self,
        *,
        owner_resource_type: str,
        owner_resource_id: str,
    ) -> ServiceAccount | None:
        result = await self.session.execute(
            select(ServiceAccount).where(
                ServiceAccount.owner_resource_type == owner_resource_type,
                ServiceAccount.owner_resource_id == owner_resource_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> list[ServiceAccount]:
        query = select(ServiceAccount).where(
            ServiceAccount.tenant_id == tenant_id,
        )
        if not include_deleted:
            query = query.where(ServiceAccount.status != "deleted")
        query = query.order_by(
            ServiceAccount.updated_at.desc(),
            ServiceAccount.service_account_id,
        )
        return list((await self.session.execute(query)).scalars())

    async def require_active_lease(
        self,
        *,
        service_account_id: uuid.UUID,
        owner_resource_type: str,
        owner_resource_id: str,
        generation: int | None = None,
    ) -> ServiceAccountLease:
        row = await self.get(service_account_id)
        if (
            row is None
            or row.status != "active"
            or row.owner_resource_type != owner_resource_type
            or row.owner_resource_id != owner_resource_id
            or (generation is not None and row.generation != generation)
        ):
            raise LookupError("service_account_unavailable")
        return ServiceAccountLease(
            service_account_id=row.service_account_id,
            tenant_id=row.tenant_id,
            generation=row.generation,
            created_by=row.created_by,
            kind=row.kind,
            owner_resource_type=row.owner_resource_type,
            owner_resource_id=row.owner_resource_id,
        )

    async def set_status(
        self,
        service_account_id: uuid.UUID,
        *,
        status: str,
        actor_user_id: uuid.UUID | None = None,
        actor_email: str | None = None,
    ) -> ServiceAccount:
        if status not in {"active", "disabled", "deleted"}:
            raise ValueError("invalid service account status")
        row = (
            await self.session.execute(
                select(ServiceAccount)
                .where(
                    ServiceAccount.service_account_id == service_account_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError("service_account_not_found")
        if row.status != status:
            previous_status = row.status
            row.status = status
            row.generation += 1
            row.updated_at = datetime.now(timezone.utc)
            row.disabled_at = (
                datetime.now(timezone.utc) if status != "active" else None
            )
            await record_audit(
                self.session,
                action=audit_actions.SERVICE_ACCOUNT_STATUS_CHANGE,
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                target_type=audit_actions.TARGET_SERVICE_ACCOUNT,
                target_id=str(service_account_id),
                target_name=row.name,
                outcome="success",
                meta={
                    "previous_status": previous_status,
                    "status": status,
                    "generation": row.generation,
                    "owner_resource_type": row.owner_resource_type,
                    "owner_resource_id": row.owner_resource_id,
                },
            )
            await self.session.flush()
        return row

    async def rotate_generation(
        self,
        service_account_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        actor_email: str | None,
    ) -> ServiceAccount:
        """Invalidate every outstanding lease without creating a secret key."""
        row = (
            await self.session.execute(
                select(ServiceAccount)
                .where(
                    ServiceAccount.service_account_id == service_account_id,
                    ServiceAccount.status != "deleted",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError("service_account_not_found")
        row.generation += 1
        row.updated_at = datetime.now(timezone.utc)
        await record_audit(
            self.session,
            action=audit_actions.SERVICE_ACCOUNT_STATUS_CHANGE,
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_type=audit_actions.TARGET_SERVICE_ACCOUNT,
            target_id=str(service_account_id),
            target_name=row.name,
            outcome="success",
            meta={
                "operation": "rotate_generation",
                "status": row.status,
                "generation": row.generation,
                "owner_resource_type": row.owner_resource_type,
                "owner_resource_id": row.owner_resource_id,
            },
        )
        await self.session.flush()
        return row

    async def bind_credential(
        self,
        *,
        tenant_id: uuid.UUID,
        service_account_id: uuid.UUID,
        credential_id: uuid.UUID,
    ) -> None:
        existing = await self.session.get(
            ServiceAccountCredential,
            (service_account_id, credential_id),
        )
        if existing is None:
            self.session.add(ServiceAccountCredential(
                tenant_id=tenant_id,
                service_account_id=service_account_id,
                credential_id=credential_id,
            ))
            await self.session.flush()

    async def credential_ids(
        self,
        service_account_id: uuid.UUID,
    ) -> tuple[uuid.UUID, ...]:
        result = await self.session.execute(
            select(ServiceAccountCredential.credential_id).where(
                ServiceAccountCredential.service_account_id
                == service_account_id
            )
        )
        return tuple(result.scalars())

    async def can_use_credential(
        self,
        *,
        service_account_id: uuid.UUID,
        credential_id: uuid.UUID,
    ) -> bool:
        return (
            await self.session.get(
                ServiceAccountCredential,
                (service_account_id, credential_id),
            )
        ) is not None

    async def resource_refs(self, service_account_id: uuid.UUID, *, include_revoked: bool = False) -> tuple[tuple[str, str], ...]:
        rows = (await self.session.execute(text(
            "SELECT resource_type, resource_id::text FROM service_account_resources "
            "WHERE service_account_id=:id " + ("" if include_revoked else "AND revoked_at IS NULL ")
            + "ORDER BY resource_type,resource_id"), {"id": service_account_id})).all()
        return tuple((row[0], row[1]) for row in rows)

    async def bind_resource(self, *, tenant_id: uuid.UUID, service_account_id: uuid.UUID,
                            resource_type: str, resource_id: uuid.UUID) -> None:
        if resource_type not in {"skill_installation", "mcp_installation"}:
            raise ValueError("Unsupported service account resource")
        # Existing revocations are tombstones: dependency refresh must not
        # silently restore a grant that an administrator explicitly removed.
        await self.session.execute(text("""INSERT INTO service_account_resources
            (tenant_id,service_account_id,resource_type,resource_id)
            SELECT :tenant,:account,:kind,:resource FROM service_accounts
            WHERE service_account_id=:account AND tenant_id=:tenant AND status='active'
            ON CONFLICT DO NOTHING"""), {"tenant": tenant_id, "account": service_account_id,
                "kind": resource_type, "resource": resource_id})

    async def revoke_resource(self, *, service_account_id: uuid.UUID,
                              resource_type: str, resource_id: uuid.UUID) -> None:
        await self.session.execute(text("""UPDATE service_account_resources SET revoked_at=now()
            WHERE service_account_id=:account AND resource_type=:kind AND resource_id=:resource"""),
            {"account": service_account_id, "kind": resource_type, "resource": resource_id})
