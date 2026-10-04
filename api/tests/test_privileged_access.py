"""Two-person privileged-support lifecycle and fail-closed regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, update

from vibecanvas_api.app import build_app
from vibecanvas_api.auth.deps import resolve_authenticated_user
from vibecanvas_api.auth.privileged_access import (
    resolve_active_privileged_access,
    validate_requested_scope,
)
from vibecanvas_api.auth.tokens import hash_token
from vibecanvas_api.authorization.stream_guard import (
    authorization_lease_is_valid,
)
from vibecanvas_api.authorization.types import Action, ResourceRef, ResourceType
from vibecanvas_api.audit.actions import AUDIT_ACTIONS
from vibecanvas_api.audit.repo import AuditRepo
from vibecanvas_api.config import config
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import AuditLog, Session
from vibecanvas_api.storage.models_privileged_access import (
    PlatformAdminEligibility,
    PrivilegedAccessRequest,
)


ORIGIN = "http://testserver"


def _csrf_headers(client: AsyncClient, audience: str = "web") -> dict[str, str]:
    csrf = client.cookies.get(f"vibecanvas-{audience}-csrf")
    assert csrf
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


async def _register(client: AsyncClient, label: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={
            "email": f"{label}-{uuid.uuid4().hex[:10]}@example.com",
            "username": label,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _grant_webauthn_step_up(raw_session: str) -> None:
    async with session_scope() as session:
        await session.execute(
            update(Session)
            .where(Session.token_hash == hash_token(raw_session))
            .values(
                authentication_strength="webauthn",
                step_up_expires_at=(
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ),
            )
        )


def test_privileged_scope_rejects_permanent_or_implicit_authority() -> None:
    with pytest.raises(
        ValueError,
        match="privileged_access_permanent_authority_forbidden",
    ):
        validate_requested_scope(
            resource_type=ResourceType.WORKFLOW,
            resource_id="workflow-1",
            actions={Action.MANAGE_ACCESS},
            sensitive_scope_confirmed=True,
        )
    with pytest.raises(
        ValueError,
        match="privileged_access_organization_scope_metadata_only",
    ):
        validate_requested_scope(
            resource_type=None,
            resource_id=None,
            actions={Action.VIEW_AUDIT},
            sensitive_scope_confirmed=True,
        )
    with pytest.raises(
        ValueError,
        match="privileged_access_sensitive_scope_confirmation_required",
    ):
        validate_requested_scope(
            resource_type=ResourceType.WORKFLOW,
            resource_id="workflow-1",
            actions={Action.UPDATE},
            sensitive_scope_confirmed=False,
        )


@pytest.mark.asyncio
async def test_privileged_support_two_person_activation_scope_and_revocation(
    pg_engine,
    openfga_allow_all,
    monkeypatch,
) -> None:
    from vibecanvas_api.routes import privileged_access as privileged_routes

    delivered_notices: list[tuple[str, str, str]] = []

    class CapturingEmailSender:
        fail = False

        def send(self, to: str, subject: str, body: str) -> None:
            if self.fail:
                raise RuntimeError("mail transport unavailable")
            delivered_notices.append((to, subject, body))

    email_sender = CapturingEmailSender()
    monkeypatch.setattr(
        privileged_routes,
        "get_email_sender",
        lambda: email_sender,
    )
    monkeypatch.setattr(config, "web_session_cookie_enabled", True)
    monkeypatch.setattr(config, "web_session_cookie_secure", False)
    monkeypatch.setattr(config.public_urls, "public_url", "")
    app = build_app()
    app.state.openfga_client = openfga_allow_all
    transport = ASGITransport(app=app)

    async with (
        AsyncClient(transport=transport, base_url=ORIGIN) as requester,
        AsyncClient(transport=transport, base_url=ORIGIN) as approver,
    ):
        requester_registration = await _register(requester, "support-requester")
        approver_registration = await _register(approver, "support-approver")
        requester_user_id = requester_registration["user"]["user_id"]
        approver_user_id = approver_registration["user"]["user_id"]
        organization_id = requester_registration["session"][
            "active_organization_id"
        ]
        requester_raw_web = requester.cookies.get("vibecanvas-web-session")
        approver_raw_web = approver.cookies.get("vibecanvas-web-session")
        assert requester_raw_web and approver_raw_web
        await _grant_webauthn_step_up(requester_raw_web)
        await _grant_webauthn_step_up(approver_raw_web)
        monkeypatch.setattr(config, "privileged_access_enabled", True)
        monkeypatch.setattr(
            config,
            "privileged_support_operator_ids",
            frozenset({requester_user_id, approver_user_id}),
        )
        monkeypatch.setattr(
            config,
            "privileged_access_bootstrap_admin_ids",
            frozenset({requester_user_id, approver_user_id}),
        )
        self_grant = await requester.put(
            f"/api/v1/auth/privileged-access/eligibilities/"
            f"{requester_user_id}",
            headers=_csrf_headers(requester),
            json={"role": "platform_support", "review_ttl_days": 30},
        )
        assert self_grant.status_code == 409, self_grant.text
        approver_eligibility = await requester.put(
            f"/api/v1/auth/privileged-access/eligibilities/"
            f"{approver_user_id}",
            headers=_csrf_headers(requester),
            json={"role": "platform_support", "review_ttl_days": 30},
        )
        assert approver_eligibility.status_code == 200, approver_eligibility.text
        requester_eligibility = await approver.put(
            f"/api/v1/auth/privileged-access/eligibilities/"
            f"{requester_user_id}",
            headers=_csrf_headers(approver),
            json={"role": "platform_support", "review_ttl_days": 30},
        )
        assert requester_eligibility.status_code == 200, requester_eligibility.text
        listed_eligibilities = await requester.get(
            "/api/v1/auth/privileged-access/eligibilities",
        )
        assert listed_eligibilities.status_code == 200
        assert {
            item["platform_user_id"]
            for item in listed_eligibilities.json()["items"]
        } == {requester_user_id, approver_user_id}

        management_context = await requester.get(
            "/api/v1/platform-management/context"
        )
        assert management_context.status_code == 200
        assert management_context.json() == {"role": "platform_support"}
        management_overview = await requester.get(
            "/api/v1/platform-management/overview"
        )
        assert management_overview.status_code == 200, management_overview.text
        overview = management_overview.json()
        assert overview["identity"]["registered_users"] >= 2
        assert overview["privacy"] == {
            "content_visible": False,
            "user_profiles_visible": False,
            "scope": "aggregate_and_lifecycle_metadata_only",
        }
        assert all("email" not in item for item in overview["organizations"])

        management_audit = await requester.get(
            "/api/v1/platform-management/audit?window_hours=24"
        )
        assert management_audit.status_code == 200, management_audit.text
        audit_report = management_audit.json()
        assert [item["category"] for item in audit_report["categories"]] == [
            "identity",
            "access_security",
            "resources",
            "data_lifecycle",
            "runtime_operations",
        ]
        assert audit_report["privacy"] == {
            "content_visible": False,
            "identities_visible": False,
            "customer_resource_identifiers_visible": False,
            "private_payload_decrypted": False,
        }
        assert all(
            item["total"] >= item["failures"] >= 0
            for item in audit_report["categories"]
        )
        assert all(
            "actor_user_id" not in event
            and "tenant_id" not in event
            and "target_id" not in event
            for event in audit_report["recent_events"]
        )
        assert {
            action
            for item in audit_report["catalog"]
            for action in item["actions"]
        } == set(AUDIT_ACTIONS)

        justification = "Customer-approved incident diagnosis for ticket scope"
        ticket_reference = "SEC-2026-0812"
        created = await requester.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests",
            headers=_csrf_headers(requester),
            json={
                "actions": ["view_metadata"],
                "duration_seconds": 300,
                "justification": justification,
                "ticket_reference": ticket_reference,
            },
        )
        assert created.status_code == 201, created.text
        request_id = created.json()["request_id"]

        async with pg_engine.connect() as connection:
            stored = (
                await connection.execute(
                    select(
                        PrivilegedAccessRequest.private_ciphertext,
                        PrivilegedAccessRequest.private_nonce,
                    ).where(
                        PrivilegedAccessRequest.request_id
                        == uuid.UUID(request_id)
                    )
                )
            ).one()
        assert justification not in stored.private_ciphertext
        assert ticket_reference not in stored.private_ciphertext
        assert stored.private_nonce

        self_approval = await requester.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests/{request_id}/approve",
            headers=_csrf_headers(requester),
            json={"sensitive_scope_confirmed": False},
        )
        assert self_approval.status_code == 409
        assert (
            self_approval.json()["detail"]
            == "privileged_access_self_approval_forbidden"
        )

        approved = await approver.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests/{request_id}/approve",
            headers=_csrf_headers(approver),
            json={"sensitive_scope_confirmed": False},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved_by_user_id"] == approver_user_id

        email_sender.fail = True
        notification_failure = await requester.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests/{request_id}/activate",
            headers=_csrf_headers(requester),
        )
        assert notification_failure.status_code == 503
        assert notification_failure.json()["detail"] == (
            "privileged_access_owner_notification_failed"
        )
        assert requester.cookies.get("vibecanvas-support-session") is None

        email_sender.fail = False
        activated = await requester.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests/{request_id}/activate",
            headers=_csrf_headers(requester),
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == "active"
        assert len(delivered_notices) == 1
        assert delivered_notices[0][0] == requester_registration["user"]["email"]
        assert request_id in delivered_notices[0][2]
        assert justification not in delivered_notices[0][2]
        assert ticket_reference not in delivered_notices[0][2]
        raw_support = requester.cookies.get("vibecanvas-support-session")
        assert raw_support
        assert requester.cookies.get("vibecanvas-web-session") == requester_raw_web

        me = await requester.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["session"]["audience"] == "support"
        assert me.json()["membership"]["role"] == "privileged_support"
        assert me.json()["privileged_access"] == {
            "active": True,
            "request_id": request_id,
            "resource_type": None,
            "resource_id": None,
            "actions": ["view_metadata"],
            "expires_at": me.json()["privileged_access"]["expires_at"],
        }
        status_probe = await requester.get(
            "/api/v1/auth/privileged-access/status"
        )
        assert status_probe.status_code == 200
        assert status_probe.json()["active"] is True

        # The exact approved capability works through the normal HTTP
        # authorization dependency; a stronger organization capability does
        # not fall back to the requester's ordinary owner relation.
        organization = await requester.get("/api/v1/organizations")
        assert organization.status_code == 200, organization.text
        audit_read = await requester.get("/api/v1/audit")
        assert audit_read.status_code == 404, audit_read.text

        async with session_scope() as session:
            support_auth = await resolve_authenticated_user(raw_support, session)
        async with session_scope() as session:
            support_session = (
                await session.execute(
                    select(Session).where(
                        Session.session_id == uuid.UUID(support_auth.session_id)
                    )
                )
            ).scalar_one()
            assert support_session.generation == support_auth.session_generation
            assert support_session.audience == "support"
            active_scope = await resolve_active_privileged_access(
                session,
                support_session,
            )
            assert active_scope is not None
            assert active_scope.request_id == request_id
            assert active_scope.request_id == support_auth.privileged_access_request_id
        resource = ResourceRef(
            ResourceType.ORGANIZATION,
            organization_id,
            organization_id,
        )
        assert await authorization_lease_is_valid(
            auth=support_auth,
            openfga_client=openfga_allow_all,
            resource=resource,
            action=Action.VIEW_METADATA,
        )
        assert not await authorization_lease_is_valid(
            auth=support_auth,
            openfga_client=openfga_allow_all,
            resource=resource,
            action=Action.VIEW_AUDIT,
        )

        # A second operator can revoke remotely. The requester's next status
        # probe clears only the invalid support cookie and preserves the
        # original Web Session instead of silently broadening the failed call.
        revoked = await approver.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests/{request_id}/revoke",
            headers=_csrf_headers(approver),
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "revoked"
        assert requester.cookies.get("vibecanvas-support-session") == raw_support
        inactive_probe = await requester.get(
            "/api/v1/auth/privileged-access/status"
        )
        assert inactive_probe.status_code == 200
        assert inactive_probe.json() == {"active": False}
        assert requester.cookies.get("vibecanvas-support-session") is None
        assert requester.cookies.get("vibecanvas-web-session") == requester_raw_web
        assert not await authorization_lease_is_valid(
            auth=support_auth,
            openfga_client=openfga_allow_all,
            resource=resource,
            action=Action.VIEW_METADATA,
        )

        current = await requester.get("/api/v1/auth/privileged-access/current")
        assert current.status_code == 200
        assert current.json()["active"] is False

        # Natural expiry is also a durable transition: the next liveness
        # probe deletes only the derived Session and records the request as
        # expired instead of leaving a misleading active row behind.
        second_created = await requester.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests",
            headers=_csrf_headers(requester),
            json={
                "actions": ["view_metadata"],
                "duration_seconds": 60,
                "justification": justification,
                "ticket_reference": f"{ticket_reference}-EXPIRY",
            },
        )
        assert second_created.status_code == 201, second_created.text
        second_request_id = second_created.json()["request_id"]
        second_approved = await approver.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests/{second_request_id}/approve",
            headers=_csrf_headers(approver),
            json={"sensitive_scope_confirmed": False},
        )
        assert second_approved.status_code == 200, second_approved.text
        second_activated = await requester.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests/{second_request_id}/activate",
            headers=_csrf_headers(requester),
        )
        assert second_activated.status_code == 200, second_activated.text
        second_support = requester.cookies.get("vibecanvas-support-session")
        assert second_support
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(PrivilegedAccessRequest)
                .where(
                    PrivilegedAccessRequest.request_id
                    == uuid.UUID(second_request_id)
                )
                .values(
                    active_expires_at=(
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    )
                )
            )
        expired_probe = await requester.get(
            "/api/v1/auth/privileged-access/status"
        )
        assert expired_probe.status_code == 200
        assert expired_probe.json() == {"active": False}
        assert requester.cookies.get("vibecanvas-support-session") is None
        assert requester.cookies.get("vibecanvas-web-session") == requester_raw_web
        async with pg_engine.connect() as connection:
            expired_status = (
                await connection.execute(
                    select(PrivilegedAccessRequest.status).where(
                        PrivilegedAccessRequest.request_id
                        == uuid.UUID(second_request_id)
                    )
                )
            ).scalar_one()
            derived_session_count = (
                await connection.execute(
                    select(Session.session_id).where(
                        Session.token_hash == hash_token(second_support)
                    )
                )
            ).scalar_one_or_none()
        assert expired_status == "expired"
        assert derived_session_count is None

        # Eligibility itself is separately reviewed and expires independently
        # of any request. Expiry removes the ability to request another scope.
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(PlatformAdminEligibility)
                .where(
                    PlatformAdminEligibility.platform_user_id
                    == uuid.UUID(requester_user_id)
                )
                .values(
                    reviewed_at=datetime.now(timezone.utc) - timedelta(days=2),
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )
            )
        eligibility_status = await requester.get(
            "/api/v1/auth/privileged-access/current"
        )
        assert eligibility_status.status_code == 200
        assert eligibility_status.json()["eligible_operator"] is False
        denied_after_review_expiry = await requester.post(
            f"/api/v1/auth/privileged-access/organizations/"
            f"{organization_id}/requests",
            headers=_csrf_headers(requester),
            json={
                "actions": ["view_metadata"],
                "duration_seconds": 60,
                "justification": justification,
                "ticket_reference": f"{ticket_reference}-ELIGIBILITY",
            },
        )
        assert denied_after_review_expiry.status_code == 404

    async with pg_engine.connect() as connection:
        audit_rows = (
            await connection.execute(
                select(AuditLog.action, AuditLog.outcome, AuditLog.meta).where(
                    AuditLog.action.in_(
                        {
                            "privileged_access.request",
                            "privileged_access.approve",
                            "privileged_access.activate",
                            "privileged_access.notify_owner",
                            "privileged_access.use",
                            "privileged_access.revoke",
                        }
                    )
                )
            )
        ).all()
    actions = {row.action for row in audit_rows}
    assert {
        "privileged_access.request",
        "privileged_access.approve",
        "privileged_access.activate",
        "privileged_access.notify_owner",
        "privileged_access.revoke",
    }.issubset(actions)
    denied_uses = [
        row
        for row in audit_rows
        if row.action == "privileged_access.use" and row.outcome == "failure"
    ]
    assert denied_uses
    async with session_scope(tenant_id=organization_id) as session:
        decrypted_denials = await AuditRepo(session).list_for_tenant(
            action="privileged_access.use",
            outcome="failure",
        )
    assert decrypted_denials
    assert decrypted_denials[0].meta["allowed"] is False
