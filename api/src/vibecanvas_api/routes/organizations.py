"""Organization switcher and generic group administration APIs."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.deps import (
    AuthContext,
    current_user,
    require_recent_step_up,
    tenant_db,
)
from vibecanvas_api.auth.organization_context import (
    require_active_organization,
)
from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.auth.session_security import set_session_cookies
from vibecanvas_api.auth.tokens import new_token
from vibecanvas_api.audit import actions as audit_actions
from vibecanvas_api.audit.context import extract_request_audit_context
from vibecanvas_api.audit.service import record_audit, record_auth_audit
from vibecanvas_api.authorization.dependencies import (
    context_for_auth,
    get_authz_service,
    mutation_coordinator_for_request,
    principal_for_auth,
)
from vibecanvas_api.authorization.service import (
    AuthzService,
    batch_resource_decisions,
)
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
    Decision,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    group_edges,
    group_membership_edges,
    organization_membership_edges,
)
from vibecanvas_api.schemas.access import ResourceAccessOut, access_from_decision
from vibecanvas_api.schemas.organization import (
    GroupListOut,
    GroupMemberListOut,
    GroupMembershipMutationOut,
    GroupOut,
    OrganizationListOut,
    OrganizationMemberOut,
    OrganizationMemberListOut,
    OrganizationOut,
    OrganizationSelfOut,
    OrganizationSwitchOut,
    ServiceAccountListOut,
    ServiceAccountOut,
    ServiceAccountStatusBody,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models_org import Group, GroupMembership
from vibecanvas_api.storage.models_service_accounts import (
    ServiceAccountCredential,
)
from vibecanvas_api.storage.repo_org import (
    GroupHierarchyError,
    GroupRepo,
    OrganizationRepo,
)
from vibecanvas_api.config import config
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo


router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")


class CreateOrganizationBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=3, max_length=63)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("organization name is required")
        return normalized

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SLUG.fullmatch(normalized):
            raise ValueError(
                "slug must contain only lowercase letters, numbers, and hyphens"
            )
        return normalized


class SwitchOrganizationBody(BaseModel):
    organization_id: uuid.UUID


class CreateGroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["department", "team"] = "team"
    parent_group_id: uuid.UUID | None = None


class UpdateGroupBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: Literal["department", "team"] | None = None
    parent_group_id: uuid.UUID | None = None


class SetGroupMemberBody(BaseModel):
    role: Literal["lead", "member"] = "member"
    status: Literal["active", "suspended"] = "active"


class UpdateOrganizationMemberBody(BaseModel):
    role: Literal["owner", "admin", "member", "guest", "auditor"]
    status: Literal["invited", "active", "suspended", "revoking", "revoked"]


class AddOrganizationMemberBody(BaseModel):
    email: EmailStr


def _organization_out(
    organization,
    *,
    active_organization_id: str,
    role: str,
    membership_status: str,
    membership_id: str,
) -> dict:
    organization_id = str(organization.tenant_id)
    return {
        "organization_id": organization_id,
        "kind": organization.kind,
        "slug": organization.slug,
        "name": organization.name,
        "membership_id": membership_id,
        "role": role,
        "status": membership_status,
        "active": organization_id == active_organization_id,
        # The switch response is followed by an organization bootstrap under
        # the rotated Session generation. Do not synthesize permissions from a
        # membership role while that authoritative OpenFGA check is pending.
        "access": ResourceAccessOut(source="refresh_required"),
    }


def _group_out(group: Group, decision: Decision) -> dict:
    return {
        "group_id": str(group.group_id),
        "organization_id": str(group.tenant_id),
        "parent_group_id": (
            str(group.parent_group_id) if group.parent_group_id else None
        ),
        "kind": group.kind,
        "name": group.name,
        "source": group.source,
        "directory_provider_id": (
            str(group.directory_provider_id)
            if group.directory_provider_id else None
        ),
        "external_id": group.external_id,
        "status": group.status,
        "created_by": str(group.created_by),
        "created_at": group.created_at,
        "updated_at": group.updated_at,
        "access": access_from_decision(decision),
    }


def _raise_group_hierarchy_http_error(exc: GroupHierarchyError) -> None:
    status_code = 409 if str(exc) == "idp_managed_group_read_only" else 400
    raise HTTPException(status_code, str(exc)) from exc


async def _require_permission(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    resource_type: ResourceType,
    resource_id: str,
    action: Action,
    consistency: ConsistencyPreference = ConsistencyPreference.MINIMIZE_LATENCY,
) -> Decision:
    decision = await service.check(
        principal_for_auth(auth),
        action,
        ResourceRef(
            resource_type,
            resource_id,
            auth.active_organization_id,
        ),
        context_for_auth(auth, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="organization_resource_not_found")
    return decision


async def _complete_decision(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    resource_type: ResourceType,
    resource_id: str,
    consistency: ConsistencyPreference = ConsistencyPreference.MINIMIZE_LATENCY,
) -> Decision:
    resource = ResourceRef(
        resource_type,
        resource_id,
        auth.active_organization_id,
    )
    decisions = await batch_resource_decisions(
        service,
        principal=principal_for_auth(auth),
        resources=(resource,),
        context=context_for_auth(auth, request, consistency=consistency),
    )
    return decisions[resource]


async def _rebind_tenant_guc(
    session: AsyncSession,
    organization_id: str,
) -> None:
    """Restore the transaction-local RLS scope after an explicit commit."""
    await session.execute(
        text("SELECT set_config('app.tenant_id', :organization_id, true)"),
        {"organization_id": organization_id},
    )


async def _service_account_out(
    session: AsyncSession,
    account,
) -> dict:
    credential_ids = list(
        (
            await session.execute(
                select(ServiceAccountCredential.credential_id).where(
                    ServiceAccountCredential.service_account_id
                    == account.service_account_id,
                )
            )
        ).scalars()
    )
    return {
        "service_account_id": str(account.service_account_id),
        "name": account.name,
        "kind": account.kind,
        "owner_resource_type": account.owner_resource_type,
        "owner_resource_id": account.owner_resource_id,
        "status": account.status,
        "generation": account.generation,
        "created_by": str(account.created_by),
        "credential_ids": [str(value) for value in credential_ids],
        "created_at": account.created_at,
        "updated_at": account.updated_at,
        "disabled_at": account.disabled_at,
    }


@router.get("", response_model=OrganizationListOut)
async def list_organizations(
    request: Request,
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    async with session_scope() as session:
        organizations = await AuthRepo(session).list_organizations_for_user(
            uuid.UUID(auth.user_id)
        )
    active_decision = await _complete_decision(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=auth.active_organization_id,
    )
    for item in organizations:
        item["active"] = (
            item["organization_id"] == auth.active_organization_id
        )
        item["access"] = (
            access_from_decision(active_decision)
            if item["active"]
            else ResourceAccessOut(source="switch_required")
        )
    return {
        "items": organizations,
        "active_organization_id": auth.active_organization_id,
        "session_generation": auth.session_generation,
    }


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=OrganizationOut,
)
async def create_business_organization(
    body: CreateOrganizationBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
) -> dict:
    mutation_ids: tuple[uuid.UUID, ...] = ()
    try:
        async with session_scope() as session:
            organization, membership = await AuthRepo(
                session
            ).create_business_organization(
                user_id=uuid.UUID(auth.user_id),
                name=body.name,
                slug=body.slug,
            )
            organization_id = str(organization.tenant_id)
            membership_id = str(membership.membership_id)
            coordinator = mutation_coordinator_for_request(
                request,
                organization_id,
            )
            mutation_ids = await enqueue_structural_delta(
                session=session,
                coordinator=coordinator,
                actor_type="user",
                actor_id=auth.user_id,
                before=frozenset(),
                after=organization_membership_edges(
                    organization_id=organization_id,
                    user_id=auth.user_id,
                    role="owner",
                    status="active",
                ),
                operation_id=uuid.uuid4().hex,
                source="business-organization-create",
            )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="organization_slug_already_exists",
        ) from exc
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await record_auth_audit(
        action=audit_actions.ORGANIZATION_CREATE,
        actor_user_id=auth.user_id,
        actor_email=auth.email,
        tenant_id=organization_id,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"organization_id": organization_id, "kind": "business"},
    )
    return {
        "organization_id": organization_id,
        "kind": "business",
        "slug": body.slug,
        "name": body.name,
        "membership_id": membership_id,
        "role": "owner",
        "status": "active",
        "active": False,
        # The new organization is not part of this Session's active tenant.
        # Its authoritative capabilities are loaded after an explicit switch.
        "access": ResourceAccessOut(source="switch_required"),
    }


@router.post("/active", response_model=OrganizationSwitchOut)
async def switch_active_organization(
    body: SwitchOrganizationBody,
    response: Response,
    auth: AuthContext = Depends(current_user),
) -> dict:
    raw_session = raw_csrf = None
    token_hash = csrf_hash = None
    if config.web_session_cookie_enabled:
        if auth.session_audience != "web":
            raise HTTPException(
                status_code=403,
                detail="primary_web_session_required",
            )
        raw_session, token_hash = new_token()
        raw_csrf, csrf_hash = new_token()
    async with session_scope() as session:
        repo = AuthRepo(session)
        switched = await repo.switch_active_organization(
            session_id=uuid.UUID(auth.session_id),
            user_id=uuid.UUID(auth.user_id),
            organization_id=body.organization_id,
            token_hash=token_hash,
            csrf_token_hash=csrf_hash,
        )
        if switched is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="organization_not_found",
            )
        # A derived extension Session remains bound to the old organization.
        # Revoke it now; the main app sync emits a fresh one-time exchange code
        # after its organization cache is reset.
        if config.web_session_cookie_enabled:
            await repo.delete_derived_sessions(switched.session_id)
        membership = await repo.get_membership(
            user_id=switched.user_id,
            organization_id=switched.active_organization_id,
        )
        # get_membership() set app.user_id but organization RLS remains
        # active-org scoped, so bind the validated target before loading it.
        organization = await _organization_for_switch(
            session,
            body.organization_id,
        )
    if membership is None or organization is None:  # pragma: no cover
        raise HTTPException(404, "organization_not_found")
    if raw_session is not None and raw_csrf is not None:
        set_session_cookies(
            response,
            audience="web",
            raw_session=raw_session,
            raw_csrf=raw_csrf,
            max_age=max(
                1,
                int((switched.expires_at - datetime.now(timezone.utc)).total_seconds()),
            ),
        )
    return _organization_out(
        organization,
        active_organization_id=str(switched.active_organization_id),
        role=membership.org_role,
        membership_status=membership.status,
        membership_id=str(membership.membership_id),
    ) | {"session_generation": int(switched.generation)}


async def _organization_for_switch(
    session: AsyncSession,
    organization_id: uuid.UUID,
):
    # Defined separately so every metadata read is visibly preceded by the
    # server-validated RLS scope switch.
    from sqlalchemy import text
    from vibecanvas_api.storage.models_org import Organization

    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(organization_id)},
    )
    return await session.get(Organization, organization_id)


@router.get(
    "/{organization_id}/me",
    response_model=OrganizationSelfOut,
)
async def get_organization_self(
    organization_id: str,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
) -> dict:
    """Return the caller's own organization and direct-group memberships."""
    require_active_organization(auth, organization_id)
    result = await OrganizationRepo(session).get_self_summary(
        uuid.UUID(auth.user_id)
    )
    if result is None:
        raise HTTPException(404, "organization_membership_not_found")
    return result


@router.get(
    "/{organization_id}/members",
    response_model=OrganizationMemberListOut,
)
async def list_organization_members(
    organization_id: str,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization_id,
        action=Action.VIEW_AUDIT,
    )
    return {"items": await OrganizationRepo(session).list_members()}


@router.post("/{organization_id}/members", response_model=OrganizationMemberOut, status_code=201)
async def add_organization_member(
    organization_id: str,
    body: AddOrganizationMemberBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(request=request, auth=auth, service=service,
        resource_type=ResourceType.ORGANIZATION, resource_id=organization_id,
        action=Action.MANAGE_MEMBERS, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    repo = OrganizationRepo(session)
    try:
        membership = await repo.add_registered_member(email=str(body.email), invited_by=uuid.UUID(auth.user_id))
    except ValueError as exc:
        raise HTTPException(404 if str(exc) == "registered_user_not_found" else 409, str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "organization_membership_already_exists") from exc
    coordinator = mutation_coordinator_for_request(request, organization_id)
    mutation_ids = await enqueue_structural_delta(session=session, coordinator=coordinator,
        actor_type="user", actor_id=auth.user_id, before=(),
        after=organization_membership_edges(organization_id=organization_id,
            user_id=str(membership.user_id), role=membership.org_role, status=membership.status),
        operation_id=uuid.uuid4().hex, source="organization-membership-add")
    user_id = membership.user_id
    await record_audit(session, action=audit_actions.ORGANIZATION_MEMBER_CHANGE,
        actor_user_id=auth.user_id, actor_email=auth.email,
        target_type=audit_actions.TARGET_ORGANIZATION, target_id=organization_id,
        target_name=None, outcome="success", audit_ctx=extract_request_audit_context(request),
        meta={"user_id": str(user_id), "role": "member", "status": "active", "operation": "add"})
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_tenant_guc(session, organization_id)
    return await repo.get_member_projection(user_id)


@router.get(
    "/{organization_id}/service-accounts",
    response_model=ServiceAccountListOut,
)
async def list_service_accounts(
    organization_id: str,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization_id,
        action=Action.VIEW_AUDIT,
    )
    rows = await ServiceAccountsRepo(session).list_for_tenant(
        uuid.UUID(organization_id),
    )
    return {
        "items": [
            await _service_account_out(session, row)
            for row in rows
        ],
    }


@router.patch(
    "/{organization_id}/service-accounts/{service_account_id}",
    response_model=ServiceAccountOut,
)
async def update_service_account_status(
    organization_id: str,
    service_account_id: uuid.UUID,
    body: ServiceAccountStatusBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization_id,
        action=Action.MANAGE_POLICY,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = ServiceAccountsRepo(session)
    try:
        row = await repo.set_status(
            service_account_id,
            status=body.status,
            actor_user_id=uuid.UUID(auth.user_id),
            actor_email=auth.email,
        )
    except LookupError as exc:
        raise HTTPException(404, "service_account_not_found") from exc
    if row.tenant_id != uuid.UUID(organization_id):
        raise HTTPException(404, "service_account_not_found")
    return await _service_account_out(session, row)


@router.post(
    "/{organization_id}/service-accounts/{service_account_id}/rotate",
    response_model=ServiceAccountOut,
)
async def rotate_service_account_generation(
    organization_id: str,
    service_account_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization_id,
        action=Action.MANAGE_POLICY,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = ServiceAccountsRepo(session)
    existing = await repo.get(service_account_id)
    if existing is None or existing.tenant_id != uuid.UUID(organization_id):
        raise HTTPException(404, "service_account_not_found")
    row = await repo.rotate_generation(
        service_account_id,
        actor_user_id=uuid.UUID(auth.user_id),
        actor_email=auth.email,
    )
    return await _service_account_out(session, row)


@router.patch(
    "/{organization_id}/members/{user_id}",
    response_model=OrganizationMemberOut,
)
async def update_organization_member(
    organization_id: str,
    user_id: uuid.UUID,
    body: UpdateOrganizationMemberBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization_id,
        action=Action.MANAGE_MEMBERS,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = OrganizationRepo(session)
    await repo.lock_membership_changes()
    actor = await repo.get_member(uuid.UUID(auth.user_id))
    if actor is None or actor.status != "active" or actor.org_role not in {"owner", "admin"}:
        raise HTTPException(403, "organization_member_management_forbidden")
    membership = await repo.get_member(user_id, include_revoked=True)
    if membership is None:
        raise HTTPException(404, "organization_membership_not_found")
    if actor.org_role != "owner" and (
        membership.org_role in {"owner", "admin"} or body.role in {"owner", "admin"}
    ):
        raise HTTPException(403, "organization_owner_required")
    before = organization_membership_edges(
        organization_id=organization_id,
        user_id=str(membership.user_id),
        role=membership.org_role,
        status=membership.status,
    )
    try:
        membership = await repo.update_member(
            membership,
            role=body.role,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    coordinator = mutation_coordinator_for_request(request, organization_id)
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=before,
        after=organization_membership_edges(
            organization_id=organization_id,
            user_id=str(membership.user_id),
            role=membership.org_role,
            status=membership.status,
        ),
        operation_id=uuid.uuid4().hex,
        source="organization-membership-update",
    )
    await record_audit(
        session,
        action=audit_actions.ORGANIZATION_MEMBER_CHANGE,
        actor_user_id=auth.user_id,
        actor_email=auth.email,
        target_type=audit_actions.TARGET_ORGANIZATION,
        target_id=organization_id,
        target_name=None,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={
            "user_id": str(user_id),
            "role": membership.org_role,
            "status": membership.status,
            "operation": "update",
        },
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_tenant_guc(session, organization_id)
    result = await repo.get_member_projection(user_id)
    if result is None:  # pragma: no cover - protected by the locked mutation
        raise HTTPException(404, "organization_membership_not_found")
    return result


@router.get(
    "/{organization_id}/groups",
    response_model=GroupListOut,
)
async def list_groups(
    organization_id: str,
    request: Request,
    include_archived: bool = False,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    context = context_for_auth(auth, request)
    authorized = await service.list_authorized_ids(
        principal_for_auth(auth),
        Action.VIEW_METADATA,
        ResourceType.GROUP,
        context,
    )
    groups = await GroupRepo(session).list_groups(
        include_archived=include_archived,
        authorized_ids=tuple(uuid.UUID(value) for value in authorized),
    )
    resources = tuple(
        ResourceRef(
            ResourceType.GROUP,
            str(group.group_id),
            auth.active_organization_id,
        )
        for group in groups
    )
    decisions = await batch_resource_decisions(
        service,
        principal=principal_for_auth(auth),
        resources=resources,
        context=context,
    )
    return {
        "items": [
            _group_out(group, decisions[resource])
            for group, resource in zip(groups, resources, strict=True)
        ]
    }


@router.post(
    "/{organization_id}/groups",
    status_code=status.HTTP_201_CREATED,
    response_model=GroupOut,
)
async def create_group(
    organization_id: str,
    body: CreateGroupBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.ORGANIZATION,
        resource_id=organization_id,
        action=Action.MANAGE_MEMBERS,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    try:
        group = await GroupRepo(session).create(
            organization_id=uuid.UUID(organization_id),
            created_by=uuid.UUID(auth.user_id),
            name=" ".join(body.name.split()),
            kind=body.kind,
            parent_group_id=body.parent_group_id,
        )
    except GroupHierarchyError as exc:
        _raise_group_hierarchy_http_error(exc)
    except IntegrityError as exc:
        raise HTTPException(409, "group_name_already_exists") from exc
    coordinator = mutation_coordinator_for_request(request, organization_id)
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=frozenset(),
        after=group_edges(
            organization_id=organization_id,
            group_id=str(group.group_id),
            parent_group_id=(
                str(group.parent_group_id)
                if group.parent_group_id is not None
                else None
            ),
            status=group.status,
        ),
        operation_id=uuid.uuid4().hex,
        source="group-create",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_tenant_guc(session, organization_id)
    decision = await _complete_decision(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.GROUP,
        resource_id=str(group.group_id),
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    return _group_out(group, decision)


@router.patch(
    "/{organization_id}/groups/{group_id}",
    response_model=GroupOut,
)
async def update_group(
    organization_id: str,
    group_id: uuid.UUID,
    body: UpdateGroupBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.GROUP,
        resource_id=str(group_id),
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = GroupRepo(session)
    group = await repo.get(group_id)
    if group is None:
        raise HTTPException(404, "group_not_found")
    before = group_edges(
        organization_id=organization_id,
        group_id=str(group.group_id),
        parent_group_id=(
            str(group.parent_group_id)
            if group.parent_group_id is not None
            else None
        ),
        status=group.status,
    )
    parent: uuid.UUID | None | object = (
        body.parent_group_id
        if "parent_group_id" in body.model_fields_set
        else ...
    )
    try:
        group = await repo.update(
            group,
            name=" ".join(body.name.split()) if body.name is not None else None,
            kind=body.kind,
            parent_group_id=parent,
        )
    except GroupHierarchyError as exc:
        _raise_group_hierarchy_http_error(exc)
    except IntegrityError as exc:
        raise HTTPException(409, "group_name_already_exists") from exc
    coordinator = mutation_coordinator_for_request(request, organization_id)
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=before,
        after=group_edges(
            organization_id=organization_id,
            group_id=str(group.group_id),
            parent_group_id=(
                str(group.parent_group_id)
                if group.parent_group_id is not None
                else None
            ),
            status=group.status,
        ),
        operation_id=uuid.uuid4().hex,
        source="group-update",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_tenant_guc(session, organization_id)
    decision = await _complete_decision(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.GROUP,
        resource_id=str(group.group_id),
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    return _group_out(group, decision)


@router.delete(
    "/{organization_id}/groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def archive_group(
    organization_id: str,
    group_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> Response:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.GROUP,
        resource_id=str(group_id),
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = GroupRepo(session)
    group = await repo.get(group_id)
    if group is None:
        raise HTTPException(404, "group_not_found")
    before = set(group_edges(
        organization_id=organization_id,
        group_id=str(group.group_id),
        parent_group_id=(
            str(group.parent_group_id)
            if group.parent_group_id is not None
            else None
        ),
        status=group.status,
    ))
    memberships = list(
        (
            await session.execute(
                select(GroupMembership).where(
                    GroupMembership.group_id == group_id,
                )
            )
        ).scalars()
    )
    for membership in memberships:
        before.update(group_membership_edges(
            organization_id=organization_id,
            group_id=str(group_id),
            user_id=str(membership.user_id),
            role=membership.group_role,
            status=membership.status,
        ))
    try:
        await repo.archive(group)
    except GroupHierarchyError as exc:
        raise HTTPException(409, str(exc)) from exc
    coordinator = mutation_coordinator_for_request(request, organization_id)
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=before,
        after=frozenset(),
        operation_id=uuid.uuid4().hex,
        source="group-archive",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{organization_id}/groups/{group_id}/members",
    response_model=GroupMemberListOut,
)
async def list_group_members(
    organization_id: str,
    group_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.GROUP,
        resource_id=str(group_id),
        action=Action.VIEW_METADATA,
    )
    repo = GroupRepo(session)
    if await repo.get(group_id) is None:
        raise HTTPException(404, "group_not_found")
    return {"items": await repo.list_members(group_id)}


@router.put(
    "/{organization_id}/groups/{group_id}/members/{user_id}",
    response_model=GroupMembershipMutationOut,
)
async def set_group_member(
    organization_id: str,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    body: SetGroupMemberBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.GROUP,
        resource_id=str(group_id),
        action=Action.MANAGE_MEMBERS,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = GroupRepo(session)
    group = await repo.get(group_id)
    if group is None:
        raise HTTPException(404, "group_not_found")
    previous = await repo.get_membership(
        group_id=group_id,
        user_id=user_id,
    )
    before = (
        group_membership_edges(
            organization_id=organization_id,
            group_id=str(group_id),
            user_id=str(user_id),
            role=previous.group_role,
            status=previous.status,
        )
        if previous is not None
        else frozenset()
    )
    try:
        membership = await repo.set_member(
            group=group,
            user_id=user_id,
            role=body.role,
            status=body.status,
        )
    except GroupHierarchyError as exc:
        _raise_group_hierarchy_http_error(exc)
    coordinator = mutation_coordinator_for_request(request, organization_id)
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=before,
        after=group_membership_edges(
            organization_id=organization_id,
            group_id=str(group_id),
            user_id=str(user_id),
            role=membership.group_role,
            status=membership.status,
        ),
        operation_id=uuid.uuid4().hex,
        source="group-membership-set",
    )
    await record_audit(
        session,
        action=audit_actions.ORGANIZATION_MEMBER_CHANGE,
        actor_user_id=auth.user_id,
        actor_email=auth.email,
        target_type=audit_actions.TARGET_ORGANIZATION,
        target_id=organization_id,
        target_name=None,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={
            "group_id": str(group_id),
            "user_id": str(user_id),
            "role": membership.group_role,
            "status": membership.status,
            "operation": "set",
        },
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return {
        "membership_id": str(membership.membership_id),
        "group_id": str(membership.group_id),
        "user_id": str(membership.user_id),
        "role": membership.group_role,
        "status": membership.status,
    }


@router.delete(
    "/{organization_id}/groups/{group_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_group_member(
    organization_id: str,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> Response:
    require_active_organization(auth, organization_id)
    await _require_permission(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.GROUP,
        resource_id=str(group_id),
        action=Action.MANAGE_MEMBERS,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    repo = GroupRepo(session)
    previous = await repo.get_membership(
        group_id=group_id,
        user_id=user_id,
    )
    if previous is None:
        raise HTTPException(404, "group_membership_not_found")
    before = group_membership_edges(
        organization_id=organization_id,
        group_id=str(group_id),
        user_id=str(user_id),
        role=previous.group_role,
        status=previous.status,
    )
    try:
        revoked = await repo.revoke_member(
            group_id=group_id,
            user_id=user_id,
        )
    except GroupHierarchyError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not revoked:
        raise HTTPException(404, "group_membership_not_found")
    coordinator = mutation_coordinator_for_request(request, organization_id)
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=before,
        after=frozenset(),
        operation_id=uuid.uuid4().hex,
        source="group-membership-revoke",
    )
    await record_audit(
        session,
        action=audit_actions.ORGANIZATION_MEMBER_CHANGE,
        actor_user_id=auth.user_id,
        actor_email=auth.email,
        target_type=audit_actions.TARGET_ORGANIZATION,
        target_id=organization_id,
        target_name=None,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={
            "group_id": str(group_id),
            "user_id": str(user_id),
            "operation": "revoke",
        },
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
