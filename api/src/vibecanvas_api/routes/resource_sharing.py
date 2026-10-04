"""Shared HTTP operations backed by the existing authorization service."""
from fastapi import HTTPException

from vibecanvas_api.authorization.dependencies import context_for_auth, principal_for_auth
from vibecanvas_api.authorization.mutations import AuthzMutationError
from vibecanvas_api.authorization.service import AuthorizationDeniedError
from vibecanvas_api.authorization.share_resolution import binding_from_share_resolution
from vibecanvas_api.authorization.types import (
    Action, ConsistencyPreference, RelationshipBinding, RelationshipSubject,
    RelationshipSubjectType,
)
from vibecanvas_api.config import config
from vibecanvas_api.schemas.access import DirectBindingListOut, DirectBindingOut
from vibecanvas_api.services.access_presentation import direct_binding_out


def require_sharing_enabled():
    if not config.resource_sharing_enabled:
        raise HTTPException(404, "resource_sharing_disabled")


async def list_resource_access(*, request, auth, service, session, resource, continuation_token=""):
    require_sharing_enabled()
    try:
        page = await service.list_bindings(
            principal_for_auth(auth), resource,
            context_for_auth(auth, request, consistency=ConsistencyPreference.HIGHER_CONSISTENCY),
            continuation_token=continuation_token,
        )
    except AuthorizationDeniedError as exc:
        raise HTTPException(404, "resource_not_found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return DirectBindingListOut(
        items=[await direct_binding_out(session, item) for item in page.bindings],
        continuation_token=page.continuation_token,
    )


async def change_resource_access(*, request, auth, service, resource, body, idempotency_key, grant):
    require_sharing_enabled()
    context = context_for_auth(auth, request, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    principal = principal_for_auth(auth)
    try:
        await service.require(principal, Action.MANAGE_ACCESS, resource, context)
        binding = binding_from_share_resolution(
            body.resolution_token, relation=body.relation, actor_user_id=auth.user_id,
            session_id=auth.session_id, resource=resource,
        ) if grant else RelationshipBinding(
            subject=RelationshipSubject(RelationshipSubjectType(body.subject_type), body.subject_id, body.subject_relation),
            relation=body.relation, resource=resource,
        )
        result = await (service.grant if grant else service.revoke)(
            principal, binding, context, idempotency_key=idempotency_key,
        )
    except AuthorizationDeniedError as exc:
        raise HTTPException(404, "resource_not_found") from exc
    except AuthzMutationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return DirectBindingOut(
        relation=result.relation, subject_type=result.subject.type.value,
        subject_id=result.subject.id, subject_relation=result.subject.relation,
    )
