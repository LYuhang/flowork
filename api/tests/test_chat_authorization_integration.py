"""Private Chat roots and durable child-resource authorization."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import text

from vibecanvas_api.app import build_app
from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.authorization.stream_guard import (
    authorization_lease_is_valid,
)
from vibecanvas_api.authorization.types import Action, ResourceRef, ResourceType
from vibecanvas_api.services.chat_workspace import chat_workspace_scope_id
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.background_jobs_repo import BackgroundJobsRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo


class _RelationshipStore:
    def __init__(self) -> None:
        self.tuples: set[OpenFgaTuple] = set()

    async def read(self, *, tuple_key, **_kwargs):
        return OpenFgaReadPage(
            (tuple_key,) if tuple_key in self.tuples else (),
        )

    async def write(self, *, writes=(), deletes=()):
        self.tuples.update(writes)
        self.tuples.difference_update(deletes)

    async def batch_check(self, checks, **_kwargs):
        return tuple(
            self._allowed(user, relation, object_)
            for user, relation, object_ in checks
        )

    async def list_objects(
        self,
        *,
        user,
        relation,
        object_type,
        **_kwargs,
    ):
        objects = {
            item.object
            for item in self.tuples
            if item.object.startswith(f"{object_type}:")
        }
        return tuple(
            object_.split(":", 1)[1]
            for object_ in sorted(objects)
            if self._allowed(user, relation, object_)
        )

    def _has(self, user: str, relation: str, object_: str) -> bool:
        return OpenFgaTuple(user, relation, object_) in self.tuples

    def _organization_for(self, object_: str) -> str | None:
        for item in self.tuples:
            if item.object == object_ and item.relation == "organization":
                return item.user
        return None

    def _organization_role(
        self,
        user: str,
        object_: str,
        roles: set[str],
    ) -> bool:
        organization = self._organization_for(object_)
        return bool(
            organization
            and any(self._has(user, role, organization) for role in roles)
        )

    def _allowed(self, user: str, relation: str, object_: str) -> bool:
        if object_.startswith("organization:"):
            return relation == "can_create_resource" and any(
                self._has(user, role, object_)
                for role in {"owner", "admin", "member"}
            )
        if not object_.startswith("chat:"):
            return False
        if relation == "can_view_metadata":
            return self._has(user, "creator", object_) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin", "auditor"},
                )
            )
        if relation == "can_delete":
            return self._has(user, "creator", object_) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin"},
                )
            )
        if relation.startswith("can_"):
            return self._has(user, "creator", object_)
        return False


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(
    client: AsyncClient,
    label: str,
) -> tuple[str, dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{label}_{uuid.uuid4().hex[:12]}@example.com",
            "username": label,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    token = response.json()["session_token"]
    me = (
        await client.get(
            "/api/v1/auth/me",
            headers=_headers(token),
        )
    ).json()
    return token, me


async def _join_active_organization(
    pg_engine,
    *,
    user_id: str,
    organization_id: str,
    role: str = "member",
) -> None:
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO org_memberships(
                    membership_id, user_id, tenant_id, org_role, status
                ) VALUES (
                    gen_random_uuid(), :user_id, :organization_id,
                    :role, 'active'
                )
                """
            ),
            {
                "user_id": uuid.UUID(user_id),
                "organization_id": uuid.UUID(organization_id),
                "role": role,
            },
        )
        await connection.execute(
            text(
                """
                UPDATE sessions
                SET tenant_id = :organization_id,
                    active_organization_id = :organization_id,
                    generation = generation + 1
                WHERE user_id = :user_id
                """
            ),
            {
                "user_id": uuid.UUID(user_id),
                "organization_id": uuid.UUID(organization_id),
            },
        )


async def _seed_chat_children(
    app_engine,
    *,
    organization_id: str,
    owner_user_id: str,
    chat_id: str,
) -> None:
    del app_engine
    async with session_scope(tenant_id=organization_id) as session:
        runs = AgentRunsRepo(session)
        await runs.create(
            run_id="run-authz",
            tenant_id=organization_id,
            chat_id=chat_id,
            creator_user_id=owner_user_id,
            client_request_id="request-authz",
            input_snapshot={"message": "authorization fixture"},
        )
        await runs.append_event(
            run_id="run-authz",
            seq=1,
            event_type="done",
            payload={},
            tenant_id=organization_id,
        )
        hitl = HitlRepo(session)
        await hitl.create_interactive_artifact(
            artifact_id="artifact-authz",
            tenant_id=organization_id,
            chat_id=chat_id,
            run_id="run-authz",
            component_type="html_preview",
            completion_mode="wait_for_submit",
            title="Artifact",
            definition_json={"type": "html_preview", "html": "<p>Review</p>"},
            artifact_ref=None,
            content_hash=None,
            hitl_request_id=None,
        )
        await hitl.create_request(
            hitl_request_id="hitl-authz",
            tenant_id=organization_id,
            chat_id=chat_id,
            run_id="run-authz",
            artifact_id="artifact-authz",
            hitl_type="post_tool_review",
            title="Review",
            prompt_text="Review",
            ui_payload_json={},
            agent_payload_json={},
            runtime_correlation_json={},
            mark_run_waiting=False,
        )
        await hitl.link_artifact_hitl("artifact-authz", "hitl-authz")
        await BackgroundJobsRepo(session).create_idempotent(
            job_id="job-authz",
            tenant_id=organization_id,
            chat_id=chat_id,
            creator_user_id=owner_user_id,
            parent_run_id="run-authz",
            runtime_type="codex",
            executor_type="runtime_task",
            tool_name="subagent",
            title="Background task",
            input_snapshot={},
            idempotency_key="job-authz",
        )


@pytest.mark.asyncio
async def test_chat_and_children_are_creator_private(
    app_engine,
    pg_engine,
    monkeypatch,
):
    store = _RelationshipStore()
    app = build_app()
    app.state.openfga_client = store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        owner_token, owner = await _register(client, "chat_owner")
        outsider_token, outsider = await _register(client, "chat_outsider")
        auditor_token, auditor = await _register(client, "chat_auditor")
        admin_token, admin = await _register(client, "chat_admin")
        await _join_active_organization(
            pg_engine,
            user_id=outsider["user_id"],
            organization_id=owner["tenant_id"],
        )
        store.tuples.add(OpenFgaTuple(
            f"user:{outsider['user_id']}",
            "member",
            f"organization:{owner['tenant_id']}",
        ))
        for role, member in (("auditor", auditor), ("admin", admin)):
            await _join_active_organization(
                pg_engine,
                user_id=member["user_id"],
                organization_id=owner["tenant_id"],
                role=role,
            )
            store.tuples.add(OpenFgaTuple(
                f"user:{member['user_id']}",
                role,
                f"organization:{owner['tenant_id']}",
            ))

        scope_id = (
            await client.get(
                "/api/v1/chats/bootstrap",
                headers=_headers(owner_token),
            )
        ).json()["carrier_scope_id"]
        chat_id = "chat-authz"
        created = await client.post(
            f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/attachments",
            files={"file": ("seed.txt", b"seed", "text/plain")},
            headers=_headers(owner_token),
        )
        assert created.status_code == 200, created.text
        assert OpenFgaTuple(
            f"organization:{owner['tenant_id']}",
            "organization",
            f"chat:{chat_id}",
        ) in store.tuples

        for token, is_admin in (
            (auditor_token, False),
            (admin_token, True),
        ):
            inventory = await client.get(
                "/api/v1/chats/inventory",
                headers=_headers(token),
            )
            assert inventory.status_code == 200, inventory.text
            item = inventory.json()["items"][0]
            assert item["chat_id"] == chat_id
            assert item["scope_id"] == scope_id
            assert "chat_context" not in item
            assert "name" not in item
            capabilities = set(item["access"]["capabilities"])
            assert "view_metadata" in capabilities
            assert "view" not in capabilities
            assert ("delete" in capabilities) is is_admin
        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "creator",
            f"chat:{chat_id}",
        ) in store.tuples

        # The workspace projection is exposed only after the Chat root and its
        # committed authorization edges exist. Its identifier is the canonical
        # chat-derived scope; it must not embed or trust a caller user id.
        owner_workspace = await client.get(
            "/api/v1/chats/workspace",
            params={"chat_id": chat_id},
            headers=_headers(owner_token),
        )
        assert owner_workspace.status_code == 200, owner_workspace.text
        assert owner_workspace.json()["workspace_scope_id"] == (
            chat_workspace_scope_id(chat_id)
        )
        assert owner_workspace.json()["chat_id"] == chat_id

        same_org_other_user = await client.get(
            "/api/v1/chats/workspace",
            params={"chat_id": chat_id},
            headers=_headers(outsider_token),
        )
        assert same_org_other_user.status_code == 404

        another_org_token, _another_org_user = await _register(
            client,
            "chat_other_org",
        )
        another_org = await client.get(
            "/api/v1/chats/workspace",
            params={"chat_id": chat_id},
            headers=_headers(another_org_token),
        )
        assert another_org.status_code == 404

        missing_chat = await client.get(
            "/api/v1/chats/workspace",
            params={"chat_id": "chat-authz-missing"},
            headers=_headers(owner_token),
        )
        assert missing_chat.status_code == 404

        listed = await client.get(
            f"/api/v1/chat-scopes/{scope_id}/chats",
            headers=_headers(owner_token),
        )
        assert listed.status_code == 200, listed.text
        access = listed.json()["items"][0]["access"]
        assert access["effective_role"] == "creator"
        assert {"view", "execute", "resume", "mount"} <= set(
            access["capabilities"]
        )

        await _seed_chat_children(
            app_engine,
            organization_id=owner["tenant_id"],
            owner_user_id=owner["user_id"],
            chat_id=chat_id,
        )
        owner_paths = (
            f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
            "/api/v1/hitl-requests/hitl-authz",
            "/api/v1/interactive-artifacts/artifact-authz",
            (
                f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}"
                "/background-jobs/job-authz"
            ),
            f"/api/v1/chats/{chat_id}/turns/run-authz/stream",
        )
        for path in owner_paths:
            response = await client.get(
                path,
                headers=_headers(owner_token),
            )
            assert response.status_code == 200, (path, response.text)

        for path in owner_paths:
            response = await client.get(
                path,
                headers=_headers(outsider_token),
            )
            assert response.status_code == 404, (path, response.text)
        for token in (auditor_token, admin_token):
            for path in owner_paths:
                response = await client.get(path, headers=_headers(token))
                assert response.status_code == 404, (path, response.text)

        outsider_scope = (
            await client.get(
                "/api/v1/chats/bootstrap",
                headers=_headers(outsider_token),
            )
        ).json()["carrier_scope_id"]
        outsider_list = await client.get(
            f"/api/v1/chat-scopes/{outsider_scope}/chats",
            headers=_headers(outsider_token),
        )
        assert outsider_list.status_code == 200
        assert outsider_list.json()["items"] == []
        cross_user_send = await client.post(
            (
                f"/api/v1/chat-scopes/{outsider_scope}/chats/"
                f"{chat_id}/messages"
            ),
            json={"role": "user", "content": "must not attach"},
            headers=_headers(outsider_token),
        )
        assert cross_user_send.status_code == 404

        async with app_engine.connect() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, false)"),
                {"tenant": owner["tenant_id"]},
            )
            session_row = (
                await connection.execute(
                    text(
                        """
                        SELECT s.session_id::text, s.generation,
                               s.authentication_strength,
                               m.membership_id::text, m.org_role, m.status
                        FROM sessions AS s
                        JOIN org_memberships AS m
                          ON m.user_id = s.user_id
                         AND m.tenant_id = s.active_organization_id
                        WHERE s.user_id = :user_id
                        """
                    ),
                    {"user_id": uuid.UUID(owner["user_id"])},
                )
            ).one()
        auth = AuthContext(
            user_id=owner["user_id"],
            tenant_id=owner["tenant_id"],
            active_organization_id=owner["tenant_id"],
            email=owner["email"],
            membership_id=session_row.membership_id,
            membership_role=session_row.org_role,
            membership_status=session_row.status,
            session_generation=int(session_row.generation),
            authentication_strength=session_row.authentication_strength,
            session_id=session_row.session_id,
        )
        chat_resource = ResourceRef(
            ResourceType.CHAT,
            chat_id,
            owner["tenant_id"],
        )
        assert await authorization_lease_is_valid(
            auth=auth,
            openfga_client=store,
            resource=chat_resource,
            action=Action.VIEW,
        )

        creator_edge = OpenFgaTuple(
            f"user:{owner['user_id']}",
            "creator",
            f"chat:{chat_id}",
        )
        store.tuples.remove(creator_edge)
        assert not await authorization_lease_is_valid(
            auth=auth,
            openfga_client=store,
            resource=chat_resource,
            action=Action.VIEW,
        )

        store.tuples.add(creator_edge)

        async with app_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE sessions SET generation = generation + 1 "
                    "WHERE session_id = CAST(:session_id AS uuid)"
                ),
                {"session_id": session_row.session_id},
            )
        assert not await authorization_lease_is_valid(
            auth=auth,
            openfga_client=store,
            resource=chat_resource,
            action=Action.VIEW,
        )

        admin_delete = await client.delete(
            f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}",
            headers=_headers(admin_token),
        )
        assert admin_delete.status_code == 200, admin_delete.text
        assert (
            await client.get(
                "/api/v1/chats/inventory",
                headers=_headers(auditor_token),
            )
        ).json()["items"] == []
