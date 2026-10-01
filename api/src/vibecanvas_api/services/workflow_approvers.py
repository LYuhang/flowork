"""Resolve configured emails to stable active organization-member IDs.

Resolve at admission, never accept a browser-supplied actor or re-resolve an
email when a decision arrives. This prevents an email change from transferring
an existing approval to a different account.
"""

from __future__ import annotations

import uuid

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.identity_protection import profile_email_lookup_digest
from vibecanvas_api.storage.models import User
from vibecanvas_api.storage.models_org import OrgMembership


async def resolve_workflow_approvers(
    session: AsyncSession,
    *,
    tenant_id: str,
    workflow: dict,
    initiator_user_id: str | None,
    require_explicit: bool,
) -> dict[str, str]:
    """Return node_id -> user_id, or reject admission before any execution."""
    result: dict[str, str] = {}
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("node_type") != "HumanApprovalNode":
            continue
        email = str(node.get("node_config", {}).get("approver_email") or "").strip()
        query = (
            select(User.user_id)
            .join(
                OrgMembership,
                OrgMembership.user_id == User.user_id,
            )
            .where(
                User.status == "active",
                OrgMembership.tenant_id == uuid.UUID(tenant_id),
                OrgMembership.status == "active",
            )
        )
        if email:
            try:
                email = validate_email(email, check_deliverability=False).normalized
            except EmailNotValidError as exc:
                raise ValueError("approval_email_invalid") from exc
            query = query.where(User.profile_email_lookup_hash == profile_email_lookup_digest(email))
        else:
            if require_explicit or not initiator_user_id:
                raise ValueError("approval_email_required")
            query = query.where(User.user_id == uuid.UUID(initiator_user_id))
        user_id = (await session.execute(query)).scalar_one_or_none()
        if user_id is None:
            # One code deliberately covers missing, disabled and foreign users.
            raise ValueError("approval_assignee_unavailable")
        result[node["node_id"]] = str(user_id)
    return result


async def notify_approval_requested(*, execution_id: str, approval_id: str) -> None:
    """Reserved integration point. The durable approval is the source of truth.

    This version intentionally sends no external user messages. A future
    notification adapter may retry independently using the two stable IDs.
    """
