"""Privacy-preserving explicit target resolution for resource sharing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal
import uuid

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.audit import actions as audit_actions
from vibecanvas_api.audit.context import extract_request_audit_context
from vibecanvas_api.audit.service import record_audit, record_detached_audit
from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.auth.ratelimit import (
    LoginRateLimitExceeded,
    LoginRateLimitUnavailable,
    consume_rate_limited_action,
)
from vibecanvas_api.authorization.dependencies import (
    authorize_resource,
    authz_service_for_session,
    context_for_auth,
    get_authz_service,
    principal_for_auth,
    scope_authz_service,
)
from vibecanvas_api.authorization.service import AuthzService
from vibecanvas_api.authorization.share_resolution import (
    ShareResolution,
    mint_share_resolution,
)
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.schemas.access import (
    ResolvedShareTargetOut,
    ShareTargetLookupIn,
    ShareTargetLookupOut,
    SharedResourceListOut,
    SharedResourceOut,
    access_from_decision,
    decision_allows_content,
)
from vibecanvas_api.security.crypto_core import keyed_lookup_digest
from vibecanvas_api.security.identity_protection import (
    decrypt_user_profile,
    profile_email_lookup_digest,
)
from vibecanvas_api.services.resource_provenance import (
    ResourceProvenanceBuilder,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import User
from vibecanvas_api.storage.models_authorization import SharedResourceProjection
from vibecanvas_api.storage.models_org import Group, Organization, OrgMembership
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
from vibecanvas_api.storage.repo_kb import KbRepo
from vibecanvas_api.storage.repo_org import GroupRepo
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


router = APIRouter(prefix="/api/v1/resource-access", tags=["resource-access"])

_RESOURCE_TYPES = {
    "workflow": ResourceType.WORKFLOW,
    "task": ResourceType.TASK,
    "deployment": ResourceType.DEPLOYMENT,
    "knowledge_base": ResourceType.KNOWLEDGE_BASE,
}
_RELATIONS = {
    "workflow": ("viewer", "editor", "operator", "manager"),
    "task": ("viewer", "editor", "operator", "manager"),
    "deployment": ("viewer", "editor", "operator", "manager"),
    "knowledge_base": ("viewer", "editor", "operator", "manager"),
}
_ShareableResourceName = Literal[
    "workflow",
    "task",
    "deployment",
    "knowledge_base",
]


async def _shared_resource_card(
    session: AsyncSession,
    *,
    resource_type: _ShareableResourceName,
    resource_id: str,
    decision,
    provenance_builder: ResourceProvenanceBuilder,
    recipient_user_id: str,
) -> SharedResourceOut | None:
    """Load recipient-visible metadata only after authoritative authz."""
    name = ""
    description = ""
    creator_user_id: uuid.UUID | None = None
    updated_at: datetime | None = None
    can_view_content = decision_allows_content(decision)

    if resource_type == "workflow":
        meta = await WorkflowRepo(session, recipient_user_id).get_meta(resource_id)
        if not meta:
            return None
        name = str(meta.get("workflow_name") or resource_id)
        description = str(meta.get("description") or "") if can_view_content else ""
        creator_user_id = meta.get("creator")
        updated_at = datetime.fromtimestamp(
            float(meta.get("updated_at") or 0),
            tz=timezone.utc,
        )
    elif resource_type == "task":
        try:
            typed_id = uuid.UUID(resource_id)
        except ValueError:
            return None
        task = await TasksRepo(session).get(typed_id)
        if task is None:
            return None
        payload = task.payload if can_view_content and isinstance(task.payload, dict) else {}
        candidate_name = payload.get("name")
        name = (
            str(candidate_name).strip()
            if isinstance(candidate_name, str) and candidate_name.strip()
            else "Scheduled run"
            if task.task_type == "scheduled_run"
            else "Batch run"
        )
        creator_user_id = task.user_id
        updated_at = task.finished_at or task.started_at or task.submitted_at
    elif resource_type == "deployment":
        try:
            typed_id = uuid.UUID(resource_id)
        except ValueError:
            return None
        deployment = await DeploymentsRepo(session).get(typed_id)
        if deployment is None:
            return None
        name = str(deployment.get("name") or resource_id)
        creator_user_id = deployment.get("user_id")
        updated_at = deployment.get("updated_at") or deployment.get("created_at")
    else:
        try:
            typed_id = uuid.UUID(resource_id)
        except ValueError:
            return None
        knowledge = await KbRepo(session).get_active(typed_id)
        if knowledge is None:
            return None
        name = knowledge.name or resource_id
        description = (knowledge.description or "") if can_view_content else ""
        creator_user_id = knowledge.user_id
        updated_at = knowledge.updated_at or knowledge.created_at

    if updated_at is None:
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return SharedResourceOut(
        resource_type=resource_type,
        resource_id=resource_id,
        name=name,
        description=description,
        updated_at=updated_at,
        access=access_from_decision(decision, source="shared"),
        provenance=await provenance_builder.build(
            creator_user_id=creator_user_id,
        ),
    )


def _masked_email(email: str) -> str:
    local, separator, domain = email.partition("@")
    if not separator:
        return ""
    visible = local[:1]
    return f"{visible}{'*' * max(3, min(len(local) - 1, 8))}@{domain}"


def _lookup_digest(target_type: str, identifier: str) -> str:
    return keyed_lookup_digest(
        domain="flowork:share-target-lookup:v1",
        components=(target_type,),
        value=identifier,
        casefold=True,
    )


def _lookup_audit_meta(body: ShareTargetLookupIn, *, result: str) -> dict:
    return {
        "target_type": body.target_type,
        "identifier_hash": _lookup_digest(
            body.target_type,
            body.identifier,
        ),
        "resolved": result == "resolved",
        # ``result`` is a generic content-bearing audit key and is therefore
        # intentionally redacted by the central policy. Use a domain-specific
        # structural key so operators can distinguish security outcomes.
        "lookup_outcome": result,
    }


async def _group_path(
    group: Group,
    by_id: dict[uuid.UUID, Group],
) -> str | None:
    names = [group.name]
    visited = {group.group_id}
    parent_id = group.parent_group_id
    while parent_id is not None:
        if parent_id in visited or len(names) >= 8:
            return None
        visited.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            return None
        names.append(parent.name)
        parent_id = parent.parent_group_id
    return " / ".join(reversed(names))


async def _resolve_user(
    session: AsyncSession,
    *,
    organization: Organization,
    identifier: str,
    actor_user_id: str,
) -> tuple[User, str, str] | None:
    try:
        email = validate_email(identifier, check_deliverability=False).normalized
    except EmailNotValidError:
        return None
    conditions = [
        User.profile_email_lookup_hash == profile_email_lookup_digest(email),
        User.status == "active",
        User.user_id != uuid.UUID(actor_user_id),
    ]
    query = select(User).where(*conditions)
    if organization.kind == "business":
        query = query.join(
            OrgMembership,
            (OrgMembership.user_id == User.user_id)
            & (OrgMembership.tenant_id == organization.tenant_id),
        ).where(OrgMembership.status == "active")
    # A personal resource may be shared with any active global account. Do
    # not join the recipient's home Organization here: organizations are
    # tenant-RLS protected, and the recipient does not become a member of the
    # owner's personal workspace. The exact global User match is the identity
    # boundary; object authorization remains the signed grant plus OpenFGA.
    rows = list((await session.execute(query.limit(2))).scalars())
    if len(rows) != 1:
        return None
    profile = await decrypt_user_profile(session, rows[0])
    return rows[0], profile.display_name or _masked_email(email), _masked_email(email)


async def _resolve_group(
    session: AsyncSession,
    service: AuthzService,
    *,
    request: Request,
    auth: AuthContext,
    organization: Organization,
    identifier: str,
) -> tuple[Group, str] | None:
    if organization.kind != "business" or not identifier.strip():
        return None
    authorized = await service.list_authorized_ids(
        principal_for_auth(auth),
        Action.VIEW_METADATA,
        ResourceType.GROUP,
        context_for_auth(
            auth,
            request,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        ),
    )
    parsed_ids: list[uuid.UUID] = []
    for value in authorized:
        try:
            parsed_ids.append(uuid.UUID(value))
        except ValueError:
            continue
    groups = await GroupRepo(session).list_groups(authorized_ids=parsed_ids)
    by_id = {group.group_id: group for group in groups}
    wanted = identifier.strip().casefold()
    matches: list[tuple[Group, str]] = []
    for group in groups:
        path = await _group_path(group, by_id)
        if path is not None and path.casefold() == wanted:
            matches.append((group, path))
    return matches[0] if len(matches) == 1 else None


@router.get("/shared", response_model=SharedResourceListOut)
async def list_shared_resources(
    request: Request,
    resource_type: _ShareableResourceName | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=5000),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
) -> SharedResourceListOut:
    """List direct resource shares visible to the authenticated recipient.

    Projection rows locate owning tenants but never authorize access. Each
    candidate is checked against OpenFGA at higher consistency under the
    owner's RLS context before any private metadata is decrypted.
    """
    last_projection_update = func.max(
        SharedResourceProjection.updated_at
    ).label("last_projection_update")
    statement = (
        select(
            SharedResourceProjection.owner_tenant_id,
            SharedResourceProjection.resource_type,
            SharedResourceProjection.resource_id,
            last_projection_update,
        )
        .where(
            SharedResourceProjection.recipient_user_id
            == uuid.UUID(auth.user_id)
        )
        .group_by(
            SharedResourceProjection.owner_tenant_id,
            SharedResourceProjection.resource_type,
            SharedResourceProjection.resource_id,
        )
        .order_by(
            desc(last_projection_update),
            SharedResourceProjection.resource_type,
            SharedResourceProjection.resource_id,
            SharedResourceProjection.owner_tenant_id,
        )
        .offset(offset)
        .limit(limit + 1)
    )
    if resource_type is not None:
        statement = statement.where(
            SharedResourceProjection.resource_type == resource_type
        )
    projection_rows = list((await session.execute(statement)).all())
    has_more = len(projection_rows) > limit
    projection_rows = projection_rows[:limit]

    by_owner: dict[uuid.UUID, list[tuple[int, str, str]]] = defaultdict(list)
    for index, row in enumerate(projection_rows):
        by_owner[row.owner_tenant_id].append((
            index,
            str(row.resource_type),
            str(row.resource_id),
        ))

    authorized_cards: list[tuple[int, SharedResourceOut]] = []
    for owner_tenant_id, candidates in by_owner.items():
        owner_id = str(owner_tenant_id)
        async with session_scope(
            tenant_id=owner_id,
            user_id=auth.user_id,
        ) as owner_session:
            organization = await owner_session.get(
                Organization,
                owner_tenant_id,
            )
            if organization is None:
                continue
            provenance_builder = ResourceProvenanceBuilder(owner_session)
            service = scope_authz_service(
                authz_service_for_session(
                    session=owner_session,
                    organization_id=owner_id,
                    openfga_client=getattr(
                        request.app.state,
                        "openfga_client",
                        None,
                    ),
                ),
                session=owner_session,
                auth=auth,
                request=request,
            )
            for index, candidate_type, resource_id in candidates:
                typed_resource = _RESOURCE_TYPES.get(candidate_type)
                if typed_resource is None:
                    continue
                authz_context = replace(
                    context_for_auth(
                        auth,
                        request,
                        consistency=(
                            ConsistencyPreference.HIGHER_CONSISTENCY
                        ),
                    ),
                    admitted_resource_organization_id=owner_id,
                    admitted_resource_type=candidate_type,
                    admitted_resource_id=resource_id,
                )
                decision = await service.check(
                    principal_for_auth(auth),
                    Action.VIEW_METADATA,
                    ResourceRef(typed_resource, resource_id, owner_id),
                    authz_context,
                )
                if not decision.allowed:
                    continue
                card = await _shared_resource_card(
                    owner_session,
                    resource_type=candidate_type,  # type: ignore[arg-type]
                    resource_id=resource_id,
                    decision=decision,
                    provenance_builder=provenance_builder,
                    recipient_user_id=auth.user_id,
                )
                if card is not None:
                    authorized_cards.append((index, card))

    authorized_cards.sort(key=lambda item: item[0])
    return SharedResourceListOut(
        items=[card for _, card in authorized_cards],
        next_offset=(offset + limit if has_more else None),
    )


@router.post(
    "/{resource_type}/{resource_id}/resolve-target",
    response_model=ShareTargetLookupOut,
)
async def resolve_share_target(
    resource_type: str,
    resource_id: str,
    body: ShareTargetLookupIn,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> ShareTargetLookupOut:
    typed_resource = _RESOURCE_TYPES.get(resource_type)
    if typed_resource is None:
        raise HTTPException(status_code=404, detail="resource_not_found")
    resource = ResourceRef(
        typed_resource,
        resource_id,
        auth.active_organization_id,
    )
    await authorize_resource(
        request=request,
        auth=auth,
        service=service,
        resource=resource,
        action=Action.MANAGE_ACCESS,
    )
    try:
        await consume_rate_limited_action(
            f"share-target:{auth.user_id}:{auth.active_organization_id}",
            max_attempts=30,
            window_seconds=60,
        )
    except LoginRateLimitExceeded as exc:
        await record_detached_audit(
            action=audit_actions.SHARE_LOOKUP,
            actor_user_id=uuid.UUID(auth.user_id),
            actor_email=None,
            tenant_id=auth.active_organization_id,
            outcome="failure",
            target_type=resource_type,
            target_id=resource_id,
            audit_ctx=extract_request_audit_context(request),
            meta=_lookup_audit_meta(body, result="rate_limited"),
        )
        raise HTTPException(
            status_code=429,
            detail="share_target_lookup_rate_limited",
            headers={"Retry-After": "60"},
        ) from exc
    except LoginRateLimitUnavailable as exc:
        await record_detached_audit(
            action=audit_actions.SHARE_LOOKUP,
            actor_user_id=uuid.UUID(auth.user_id),
            actor_email=None,
            tenant_id=auth.active_organization_id,
            outcome="failure",
            target_type=resource_type,
            target_id=resource_id,
            audit_ctx=extract_request_audit_context(request),
            meta=_lookup_audit_meta(body, result="rate_limit_unavailable"),
        )
        raise HTTPException(
            status_code=503,
            detail="share_target_lookup_unavailable",
        ) from exc

    organization = await session.get(
        Organization,
        uuid.UUID(auth.active_organization_id),
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="resource_not_found")

    target: ResolvedShareTargetOut | None = None
    subject_type = body.target_type
    subject_id = ""
    subject_relation: str | None = None
    display_name = ""
    detail = ""
    if body.target_type == "user":
        resolved = await _resolve_user(
            session,
            organization=organization,
            identifier=body.identifier,
            actor_user_id=auth.user_id,
        )
        if resolved is not None:
            user, display_name, detail = resolved
            subject_id = str(user.user_id)
    elif body.target_type == "group":
        resolved_group = await _resolve_group(
            session,
            service,
            request=request,
            auth=auth,
            organization=organization,
            identifier=body.identifier,
        )
        if resolved_group is not None:
            group, path = resolved_group
            subject_id = str(group.group_id)
            subject_relation = "member"
            display_name = group.name
            detail = path
    elif organization.kind == "business":
        subject_id = str(organization.tenant_id)
        subject_relation = "member"
        display_name = organization.name
        detail = "Entire organization"

    if subject_id:
        relations = _RELATIONS[resource_type]
        if organization.kind == "personal":
            # Cross-personal recipients are Guests, never replacement owners.
            relations = tuple(item for item in relations if item != "manager")
        if body.target_type == "organization":
            relations = ("viewer",)
        resolution = ShareResolution(
            actor_user_id=auth.user_id,
            session_id=auth.session_id,
            owner_organization_id=auth.active_organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_relation=subject_relation,
            allowed_relations=relations,
        )
        target = ResolvedShareTargetOut(
            target_type=body.target_type,
            display_name=display_name,
            detail=detail,
            resolution_token=mint_share_resolution(resolution),
            allowed_relations=list(relations),
        )

    await record_audit(
        session,
        action=audit_actions.SHARE_LOOKUP,
        actor_user_id=uuid.UUID(auth.user_id),
        actor_email=None,
        target_type=resource_type,
        target_id=resource_id,
        outcome="success" if target is not None else "failure",
        audit_ctx=extract_request_audit_context(request),
        meta=_lookup_audit_meta(
            body,
            result="resolved" if target is not None else "not_found",
        ),
    )
    return ShareTargetLookupOut(target=target)
