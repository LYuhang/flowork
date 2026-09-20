"""OpenFGA-backed implementation of the application-owned AuthzService."""

from __future__ import annotations

import base64
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
import json
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .openfga_client import (
    OpenFgaHttpClient,
    OpenFgaUnavailableError,
)
from .openfga_model import (
    ACTION_RELATIONS,
    OPENFGA_OBJECT_TYPES,
    SHARE_RELATION_SUBJECTS,
    action_relation,
    effective_role,
    openfga_object,
    openfga_principal,
    validate_share_binding,
)
from .parent_resolvers import resolve_authorization_root
from .service import AuthorizationDeniedError
from .types import (
    Action,
    AuthorizationCheck,
    AuthzRequestContext,
    BindingPage,
    ConsistencyPreference,
    Decision,
    PrincipalRef,
    PrincipalType,
    RelationshipBinding,
    RelationshipSubject,
    RelationshipSubjectType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.storage.models_authorization import (
    AuthzEdgeRevision,
    AuthzMutation,
)
from vibecanvas_api.storage.models import User
from vibecanvas_api.storage.models_org import Group, Organization, OrgMembership

if TYPE_CHECKING:
    from .mutations import AuthzMutationCoordinator


class OpenFgaAuthzService:
    """Translate stable Flowork actions to one pinned OpenFGA model."""

    def __init__(
        self,
        session: AsyncSession,
        client: OpenFgaHttpClient,
        *,
        mutation_coordinator: AuthzMutationCoordinator | None = None,
    ) -> None:
        self._session = session
        self._client = client
        self._mutations = mutation_coordinator
        from .mutations import RevocationGuard

        self._revocation_guard = RevocationGuard(session)

    async def check(
        self,
        principal: PrincipalRef,
        action: Action,
        resource: ResourceRef,
        context: AuthzRequestContext,
    ) -> Decision:
        root, denial = await self._resolve_and_validate(
            principal, resource, context
        )
        if denial is not None:
            return denial
        assert root is not None
        relations = ACTION_RELATIONS.get(root.type)
        if relations is None or action not in relations:
            return Decision(False, reason_code="unsupported_action")

        user = openfga_principal(principal.type.value, principal.id)
        object_ = openfga_object(root.type, root.id)
        actions = tuple(relations)
        guarded = {
            item
            for item in actions
            if await self._revocation_guard.denies(
                principal=principal,
                action=item,
                resource=root,
                membership_role=context.membership_role,
            )
        }
        query_actions = tuple(item for item in actions if item not in guarded)
        queried = await self._client.batch_check(
            [
                (user, relations[item], object_)
                for item in query_actions
            ],
            consistency=context.consistency,
        )
        allowed_by_action = dict(zip(query_actions, queried, strict=True))
        capabilities = frozenset(
            item
            for item in actions
            if allowed_by_action.get(item, False)
        )
        is_allowed = action in capabilities
        return Decision(
            is_allowed,
            capabilities=capabilities,
            effective_role=effective_role(root.type, capabilities),
            reason_code="openfga_allow" if is_allowed else "openfga_deny",
        )

    async def require(
        self,
        principal: PrincipalRef,
        action: Action,
        resource: ResourceRef,
        context: AuthzRequestContext,
    ) -> Decision:
        decision = await self.check(principal, action, resource, context)
        if not decision.allowed:
            raise AuthorizationDeniedError(decision)
        return decision

    async def batch_check(
        self,
        checks: Sequence[AuthorizationCheck],
    ) -> tuple[Decision, ...]:
        if not checks:
            return ()
        decisions: list[Decision | None] = [None] * len(checks)
        groups: dict[
            ConsistencyPreference,
            list[tuple[int, AuthorizationCheck, str, str, str]],
        ] = defaultdict(list)
        resolved: list[
            tuple[
                int,
                AuthorizationCheck,
                ResourceRef,
                str,
                str,
                str,
            ]
        ] = []

        for index, item in enumerate(checks):
            root, denial = await self._resolve_and_validate(
                item.principal,
                item.resource,
                item.context,
            )
            if denial is not None:
                decisions[index] = denial
                continue
            assert root is not None
            try:
                relation = action_relation(root.type, item.action)
            except ValueError:
                decisions[index] = Decision(
                    False,
                    reason_code="unsupported_action",
                )
                continue
            resolved.append((
                index,
                item,
                root,
                openfga_principal(
                    item.principal.type.value,
                    item.principal.id,
                ),
                relation,
                openfga_object(root.type, root.id),
            ))

        guarded = await self._revocation_guard.denies_many(
            checks=tuple(
                (
                    item.principal,
                    item.action,
                    root,
                    item.context.membership_role,
                )
                for _, item, root, _, _, _ in resolved
            ),
        )
        for entry, is_guarded in zip(resolved, guarded, strict=True):
            index, item, _root, user, relation, object_ = entry
            if is_guarded:
                decisions[index] = Decision(
                    False,
                    reason_code="revocation_pending",
                )
                continue
            groups[item.consistency].append((
                index,
                item,
                user,
                relation,
                object_,
            ))

        for consistency, group in groups.items():
            results = await self._client.batch_check(
                [
                    (user, relation, object_)
                    for _, _, user, relation, object_ in group
                ],
                consistency=consistency,
            )
            for (index, item, _, _, _), allowed in zip(
                group,
                results,
                strict=True,
            ):
                decisions[index] = Decision(
                    allowed,
                    capabilities=(
                        frozenset({item.action}) if allowed else frozenset()
                    ),
                    reason_code=(
                        "openfga_allow" if allowed else "openfga_deny"
                    ),
                )

        if any(item is None for item in decisions):
            raise OpenFgaUnavailableError("authorization_invalid_response")
        return tuple(item for item in decisions if item is not None)

    async def list_authorized_ids(
        self,
        principal: PrincipalRef,
        action: Action,
        resource_type: ResourceType,
        context: AuthzRequestContext,
    ) -> tuple[str, ...]:
        denial = self._validate_context(
            principal,
            ResourceRef(resource_type, "list", context.active_organization_id),
            context,
        )
        if denial is not None:
            return ()
        object_type = OPENFGA_OBJECT_TYPES.get(resource_type)
        if object_type is None:
            return ()
        try:
            relation = action_relation(resource_type, action)
        except ValueError:
            return ()
        # Callers MUST intersect these opaque IDs with a tenant-bound Postgres
        # query. OpenFGA determines relationship visibility; RLS remains the
        # hard organization boundary.
        object_ids = await self._client.list_objects(
            user=openfga_principal(principal.type.value, principal.id),
            relation=relation,
            object_type=object_type,
            consistency=context.consistency,
        )
        denied_ids = await self._revocation_guard.denied_resource_ids(
            principal=principal,
            action=action,
            resource_type=resource_type,
            resource_ids=object_ids,
            organization_id=context.active_organization_id,
            membership_role=context.membership_role,
        )
        return tuple(
            object_id
            for object_id in object_ids
            if object_id not in denied_ids
        )

    async def list_bindings(
        self,
        principal: PrincipalRef,
        resource: ResourceRef,
        context: AuthzRequestContext,
        *,
        continuation_token: str = "",
    ) -> BindingPage:
        root = await self.resolve_parent(resource)
        if root is None:
            raise AuthorizationDeniedError(
                Decision(False, reason_code="resource_not_found")
            )
        await self.require(
            principal,
            Action.MANAGE_ACCESS,
            root,
            context,
        )
        allowed_relations = SHARE_RELATION_SUBJECTS.get(root.type)
        if allowed_relations is None:
            return BindingPage(())
        cursor = _decode_binding_cursor(continuation_token)
        query = (
            select(AuthzMutation)
            .join(
                AuthzEdgeRevision,
                and_(
                    AuthzEdgeRevision.tenant_id
                    == AuthzMutation.tenant_id,
                    AuthzEdgeRevision.object_type
                    == AuthzMutation.object_type,
                    AuthzEdgeRevision.object_id
                    == AuthzMutation.object_id,
                    AuthzEdgeRevision.relation
                    == AuthzMutation.relation,
                    AuthzEdgeRevision.subject_type
                    == AuthzMutation.subject_type,
                    AuthzEdgeRevision.subject_id
                    == AuthzMutation.subject_id,
                    AuthzEdgeRevision.subject_relation
                    == func.coalesce(AuthzMutation.subject_relation, ""),
                    AuthzEdgeRevision.current_revision
                    == AuthzMutation.edge_revision,
                ),
            )
            .where(
                AuthzMutation.object_type
                == OPENFGA_OBJECT_TYPES[root.type],
                AuthzMutation.object_id == root.id,
                AuthzMutation.kind == "direct_binding",
                AuthzMutation.desired_state == "present",
                AuthzMutation.status == "applied",
            )
            .order_by(
                AuthzMutation.requested_at,
                AuthzMutation.mutation_id,
            )
        )
        if cursor is not None:
            requested_at, mutation_id = cursor
            query = query.where(or_(
                AuthzMutation.requested_at > requested_at,
                and_(
                    AuthzMutation.requested_at == requested_at,
                    AuthzMutation.mutation_id > mutation_id,
                ),
            ))
        rows = list(
            (
                await self._session.execute(query.limit(101))
            ).scalars()
        )
        bindings: list[RelationshipBinding] = []
        for item in rows[:100]:
            if item.relation not in allowed_relations:
                continue
            bindings.append(RelationshipBinding(
                subject=RelationshipSubject(
                    type=RelationshipSubjectType(item.subject_type),
                    id=item.subject_id,
                    relation=item.subject_relation,
                ),
                relation=item.relation,
                resource=root,
            ))
        next_token = (
            _encode_binding_cursor(rows[99])
            if len(rows) > 100
            else ""
        )
        return BindingPage(tuple(bindings), next_token)

    async def grant(
        self,
        principal: PrincipalRef,
        binding: RelationshipBinding,
        context: AuthzRequestContext,
        *,
        idempotency_key: str,
    ) -> RelationshipBinding:
        return await self._request_binding_mutation(
            principal=principal,
            binding=binding,
            context=context,
            idempotency_key=idempotency_key,
            desired_present=True,
        )

    async def revoke(
        self,
        principal: PrincipalRef,
        binding: RelationshipBinding,
        context: AuthzRequestContext,
        *,
        idempotency_key: str,
    ) -> RelationshipBinding:
        return await self._request_binding_mutation(
            principal=principal,
            binding=binding,
            context=context,
            idempotency_key=idempotency_key,
            desired_present=False,
        )

    async def resolve_parent(
        self,
        resource: ResourceRef,
    ) -> ResourceRef | None:
        return await resolve_authorization_root(self._session, resource)

    async def _request_binding_mutation(
        self,
        *,
        principal: PrincipalRef,
        binding: RelationshipBinding,
        context: AuthzRequestContext,
        idempotency_key: str,
        desired_present: bool,
    ) -> RelationshipBinding:
        validate_share_binding(binding)
        root = await self.resolve_parent(binding.resource)
        if root is None or root != binding.resource:
            raise AuthorizationDeniedError(
                Decision(False, reason_code="resource_not_found")
            )
        await self.require(
            principal,
            Action.MANAGE_ACCESS,
            root,
            context,
        )
        if desired_present:
            await self._validate_binding_subject(binding)
        if self._mutations is None:
            # Never bypass the durable-intent ledger by writing OpenFGA
            # directly, even if the control plane itself is healthy.
            raise OpenFgaUnavailableError(
                "authorization_mutation_unavailable"
            )
        await self._mutations.request_binding(
            actor=principal,
            binding=binding,
            desired_present=desired_present,
            idempotency_key=idempotency_key,
        )
        return binding

    async def _validate_binding_subject(
        self,
        binding: RelationshipBinding,
    ) -> None:
        """Reject syntactically valid but foreign/nonexistent principals."""
        subject = binding.subject
        organization_id = uuid.UUID(binding.resource.organization_id)
        if subject.type is RelationshipSubjectType.USER:
            try:
                subject_id = uuid.UUID(subject.id)
            except ValueError as exc:
                raise ValueError("share subject does not belong to organization") from exc
            organization = await self._session.get(
                Organization,
                organization_id,
            )
            if organization is None:
                raise ValueError("share organization does not exist")
            if organization.kind == "personal":
                if binding.relation == "manager":
                    raise ValueError(
                        "personal guest cannot receive manager access"
                    )
                user = await self._session.get(User, subject_id)
                if user is None or user.status != "active":
                    raise ValueError("share subject is not eligible")
            else:
                membership = (
                    await self._session.execute(
                        select(OrgMembership.membership_id).where(
                            OrgMembership.tenant_id == organization_id,
                            OrgMembership.user_id == subject_id,
                            OrgMembership.status == "active",
                        )
                    )
                ).scalar_one_or_none()
                if membership is None:
                    raise ValueError(
                        "share subject does not belong to organization"
                    )
            return
        if subject.type is RelationshipSubjectType.GROUP:
            try:
                subject_id = uuid.UUID(subject.id)
            except ValueError as exc:
                raise ValueError("share group does not belong to organization") from exc
            group = (
                await self._session.execute(
                    select(Group.group_id).where(
                        Group.tenant_id == organization_id,
                        Group.group_id == subject_id,
                        Group.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if group is None:
                raise ValueError(
                    "share group does not belong to organization"
                )
            return
        if subject.type is RelationshipSubjectType.ORGANIZATION:
            if (
                subject.id != binding.resource.organization_id
                or subject.relation != "member"
            ):
                raise ValueError(
                    "share organization must be the resource organization"
                )
            return
        if subject.type is RelationshipSubjectType.SERVICE_ACCOUNT:
            try:
                subject_id = uuid.UUID(subject.id)
            except ValueError as exc:
                raise ValueError(
                    "service account does not belong to organization"
                ) from exc
            from vibecanvas_api.storage.models_service_accounts import ServiceAccount

            account = (
                await self._session.execute(
                    select(ServiceAccount.service_account_id).where(
                        ServiceAccount.tenant_id == organization_id,
                        ServiceAccount.service_account_id == subject_id,
                        ServiceAccount.status != "deleted",
                    )
                )
            ).scalar_one_or_none()
            if account is None:
                raise ValueError(
                    "service account does not belong to organization"
                )
            return
        raise ValueError("unsupported authorization subject")

    async def _resolve_and_validate(
        self,
        principal: PrincipalRef,
        resource: ResourceRef,
        context: AuthzRequestContext,
    ) -> tuple[ResourceRef | None, Decision | None]:
        denial = self._validate_principal_context(principal, context)
        if denial is not None:
            return None, denial
        scoped_resource = resource
        if (
            context.admitted_resource_organization_id
            and resource.organization_id == context.active_organization_id
        ):
            scoped_resource = ResourceRef(
                resource.type,
                resource.id,
                context.admitted_resource_organization_id,
            )
        root = await self.resolve_parent(scoped_resource)
        if root is None:
            return None, Decision(False, reason_code="resource_not_found")
        denial = self._validate_context(principal, root, context)
        if denial is not None:
            return None, denial
        return root, None

    @staticmethod
    def _validate_principal_context(
        principal: PrincipalRef,
        context: AuthzRequestContext,
    ) -> Decision | None:
        if principal.type is PrincipalType.USER:
            if context.membership_status != "active":
                return Decision(
                    False,
                    reason_code="inactive_organization_membership",
                )
        elif principal.type is not PrincipalType.SERVICE_ACCOUNT:
            return Decision(False, reason_code="unsupported_principal")
        return None

    @classmethod
    def _validate_context(
        cls,
        principal: PrincipalRef,
        resource: ResourceRef,
        context: AuthzRequestContext,
    ) -> Decision | None:
        denial = cls._validate_principal_context(principal, context)
        if denial is not None:
            return denial
        if resource.organization_id == context.active_organization_id:
            return None
        if (
            resource.organization_id
            == context.admitted_resource_organization_id
            and resource.type.value == context.admitted_resource_type
            and resource.id == context.admitted_resource_id
        ):
            return None
        if resource.organization_id != context.active_organization_id:
            return Decision(False, reason_code="organization_mismatch")
        return None


def _encode_binding_cursor(mutation: AuthzMutation) -> str:
    payload = json.dumps(
        {
            "requested_at": mutation.requested_at.isoformat(),
            "mutation_id": str(mutation.mutation_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_binding_cursor(
    value: str,
) -> tuple[datetime, uuid.UUID] | None:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        requested_at = datetime.fromisoformat(str(payload["requested_at"]))
        mutation_id = uuid.UUID(str(payload["mutation_id"]))
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("invalid authorization continuation token") from exc
    if requested_at.tzinfo is None:
        raise ValueError("invalid authorization continuation token")
    return requested_at, mutation_id
