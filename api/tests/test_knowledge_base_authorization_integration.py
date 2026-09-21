"""Knowledge Base authorization, child inheritance, sharing, and revoke."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from vibecanvas_api.app import build_app
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.config import config
from vibecanvas_api.background_tasks.kb_indexer import kb_index_file_task
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_kb import KbRepo


class _RelationshipStore:
    def __init__(self) -> None:
        self.tuples: set[OpenFgaTuple] = set()

    async def read(self, *, tuple_key, **_kwargs):
        return OpenFgaReadPage(
            (tuple_key,) if tuple_key in self.tuples else ()
        )

    async def write(self, *, writes=(), deletes=()):
        self.tuples.update(writes)
        self.tuples.difference_update(deletes)

    async def close(self) -> None:
        return None

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

    def _role(self, user: str, object_: str, roles: set[str]) -> bool:
        return any(self._has(user, role, object_) for role in roles)

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
            if relation == "can_create_resource":
                return self._role(
                    user,
                    object_,
                    {"owner", "admin", "member"},
                )
            return False
        if not object_.startswith("knowledge_base:"):
            return False
        content_roles = {"viewer", "editor", "operator", "manager"}
        if relation == "can_view_metadata":
            return self._role(user, object_, content_roles) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin", "auditor"},
                )
            )
        if relation == "can_view":
            return self._role(user, object_, content_roles)
        if relation == "can_update":
            return self._role(user, object_, {"editor", "manager"})
        if relation == "can_use":
            return self._role(user, object_, {"operator", "manager"})
        if relation in {"can_delete", "can_manage_access"}:
            return self._role(user, object_, {"manager"}) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin"},
                )
            )
        return False


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


async def _register(client: AsyncClient, label: str) -> tuple[str, dict]:
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


class _ObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(
        self,
        key: str,
        data: bytes,
        _content_type: str,
    ) -> str:
        self.objects[key] = data
        return key


@pytest.mark.asyncio
async def test_knowledge_base_roles_children_share_and_revoke(
    pg_engine,
    monkeypatch,
):
    async def _enqueue_background_job(*_args, **_kwargs):
        return None

    monkeypatch.setattr(config, "resource_sharing_enabled", True)
    store = _RelationshipStore()
    object_store = _ObjectStore()
    monkeypatch.setattr(
        "vibecanvas_api.routes.kb.get_object_store",
        lambda: object_store,
    )
    monkeypatch.setattr(
        "vibecanvas_api.routes.kb.enqueue_background_job_async",
        _enqueue_background_job,
    )
    app = build_app()
    app.state.openfga_client = store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        owner_token, owner = await _register(client, "kb_owner")
        viewer_token, viewer = await _register(client, "kb_viewer")
        editor_token, editor = await _register(client, "kb_editor")
        operator_token, operator = await _register(client, "kb_operator")
        outsider_token, outsider = await _register(client, "kb_outsider")
        guest_token, guest = await _register(client, "kb_guest")
        auditor_token, auditor = await _register(client, "kb_auditor")
        admin_token, admin = await _register(client, "kb_admin")
        for member in (viewer, editor, operator, outsider):
            await _join_active_organization(
                pg_engine,
                user_id=member["user_id"],
                organization_id=owner["tenant_id"],
            )
        await _join_active_organization(
            pg_engine,
            user_id=guest["user_id"],
            organization_id=owner["tenant_id"],
            role="guest",
        )
        store.tuples.add(OpenFgaTuple(
            f"user:{guest['user_id']}",
            "guest",
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

        guest_create = await client.post(
            "/api/v1/kb",
            json={"name": "Guest cannot create"},
            headers=_headers(guest_token),
        )
        assert guest_create.status_code == 404

        created = await client.post(
            "/api/v1/kb",
            json={
                "name": "Private knowledge",
                "description": "private retrieval instructions",
            },
            headers=_headers(owner_token),
        )
        assert created.status_code == 201, created.text
        knowledge_base = created.json()
        kb_id = knowledge_base["id"]
        assert knowledge_base["access"]["effective_role"] == "manager"
        assert "manage_access" in knowledge_base["access"]["capabilities"]
        assert knowledge_base["provenance"]["ownership_scope"] == "personal"
        assert knowledge_base["provenance"]["origin_type"] == "created"
        assert knowledge_base["provenance"]["owner"]["display_name"] == (
            "kb_owner"
        )
        assert OpenFgaTuple(
            f"organization:{owner['tenant_id']}",
            "organization",
            f"knowledge_base:{kb_id}",
        ) in store.tuples

        for token, is_admin in (
            (auditor_token, False),
            (admin_token, True),
        ):
            inventory = await client.get("/api/v1/kb", headers=_headers(token))
            assert inventory.status_code == 200, inventory.text
            item = inventory.json()[0]
            assert item["id"] == kb_id
            assert item["name"] == "Private knowledge"
            assert item["description"] is None
            capabilities = set(item["access"]["capabilities"])
            assert "view_metadata" in capabilities
            assert "view" not in capabilities
            assert "use" not in capabilities
            assert ("manage_access" in capabilities) is is_admin
            assert (
                await client.get(
                    f"/api/v1/kb/{kb_id}",
                    headers=_headers(token),
                )
            ).status_code == 404
        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "manager",
            f"knowledge_base:{kb_id}",
        ) in store.tuples

        hidden = await client.get(
            "/api/v1/kb",
            headers=_headers(viewer_token),
        )
        assert hidden.status_code == 200
        assert hidden.json() == []
        outsider_detail = await client.get(
            f"/api/v1/kb/{kb_id}",
            headers=_headers(outsider_token),
        )
        assert outsider_detail.status_code == 404

        async def grant(relation: str, subject: dict) -> None:
            resolved = await client.post(
                f"/api/v1/resource-access/knowledge_base/{kb_id}/resolve-target",
                json={
                    "target_type": "user",
                    "identifier": subject["email"],
                },
                headers=_headers(owner_token),
            )
            assert resolved.status_code == 200, resolved.text
            target = resolved.json()["target"]
            assert target is not None
            response = await client.post(
                f"/api/v1/kb/{kb_id}/access",
                json={
                    "relation": relation,
                    "resolution_token": target["resolution_token"],
                },
                headers=_headers(
                    owner_token,
                    **{
                        "Idempotency-Key": (
                            f"grant-kb-{relation}-{subject['user_id']}"
                        )
                    },
                ),
            )
            assert response.status_code == 201, response.text

        await grant("viewer", viewer)
        await grant("editor", editor)
        await grant("operator", operator)

        shared = await client.get(
            "/api/v1/resource-access/shared?resource_type=knowledge_base",
            headers=_headers(viewer_token),
        )
        assert shared.status_code == 200, shared.text
        assert [item["resource_id"] for item in shared.json()["items"]] == [
            kb_id
        ]
        assert shared.json()["items"][0]["name"] == "Private knowledge"
        assert shared.json()["items"][0]["access"]["effective_role"] == (
            "viewer"
        )

        visible = await client.get(
            "/api/v1/kb",
            headers=_headers(viewer_token),
        )
        assert [item["id"] for item in visible.json()] == [kb_id]
        viewer_access = visible.json()[0]["access"]
        assert viewer_access["effective_role"] == "viewer"
        assert "view" in viewer_access["capabilities"]
        assert "update" not in viewer_access["capabilities"]
        assert "use" not in viewer_access["capabilities"]

        viewer_update = await client.patch(
            f"/api/v1/kb/{kb_id}",
            json={"name": "viewer cannot rename"},
            headers=_headers(viewer_token),
        )
        assert viewer_update.status_code == 404
        editor_update = await client.patch(
            f"/api/v1/kb/{kb_id}",
            json={"name": "Editor renamed"},
            headers=_headers(editor_token),
        )
        assert editor_update.status_code == 200, editor_update.text
        assert editor_update.json()["access"]["effective_role"] == "editor"
        operator_update = await client.patch(
            f"/api/v1/kb/{kb_id}",
            json={"name": "operator cannot rename"},
            headers=_headers(operator_token),
        )
        assert operator_update.status_code == 404

        viewer_search = await client.post(
            "/api/v1/kb/search",
            json={"kb_ids": [kb_id], "query": "secret"},
            headers=_headers(viewer_token),
        )
        assert viewer_search.status_code == 404
        operator_search = await client.post(
            "/api/v1/kb/search",
            json={
                "kb_ids": [kb_id],
                "query": "secret",
            },
            headers=_headers(operator_token),
        )
        assert operator_search.status_code == 200, operator_search.text
        assert operator_search.json() == {"results": []}

        uploaded = await client.post(
            f"/api/v1/kb/{kb_id}/files",
            files={"file": ("notes.txt", b"private notes", "text/plain")},
            headers=_headers(editor_token),
        )
        assert uploaded.status_code == 200, uploaded.text
        file_id = uploaded.json()["file_id"]
        assert object_store.objects

        files = await client.get(
            f"/api/v1/kb/{kb_id}/files",
            headers=_headers(viewer_token),
        )
        assert files.status_code == 200, files.text
        assert files.json()[0]["id"] == file_id
        assert files.json()[0]["access"]["effective_role"] == "viewer"

        editor_delete_file = await client.delete(
            f"/api/v1/kb/{kb_id}/files/{file_id}",
            headers=_headers(editor_token),
        )
        assert editor_delete_file.status_code == 404
        owner_delete_file = await client.delete(
            f"/api/v1/kb/{kb_id}/files/{file_id}",
            headers=_headers(owner_token),
        )
        assert owner_delete_file.status_code == 204

        listed = await client.get(
            f"/api/v1/kb/{kb_id}/access",
            headers=_headers(owner_token),
        )
        assert listed.status_code == 200, listed.text
        assert {
            (item["relation"], item["subject_id"])
            for item in listed.json()["items"]
        } == {
            ("viewer", viewer["user_id"]),
            ("editor", editor["user_id"]),
            ("operator", operator["user_id"]),
        }

        structural_revoke = await client.request(
            "DELETE",
            f"/api/v1/kb/{kb_id}/access",
            json={
                "relation": "manager",
                "subject_type": "user",
                "subject_id": owner["user_id"],
                "subject_relation": None,
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "cannot-revoke-kb-creator"},
            ),
        )
        assert structural_revoke.status_code == 409

        viewer_binding = {
            "relation": "viewer",
            "subject_type": "user",
            "subject_id": viewer["user_id"],
            "subject_relation": None,
        }
        revoked = await client.request(
            "DELETE",
            f"/api/v1/kb/{kb_id}/access",
            json=viewer_binding,
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "revoke-kb-viewer"},
            ),
        )
        assert revoked.status_code == 200, revoked.text
        assert (
            await client.get(
                f"/api/v1/kb/{kb_id}",
                headers=_headers(viewer_token),
            )
        ).status_code == 404
        assert (
            await client.get(
                "/api/v1/kb",
                headers=_headers(viewer_token),
            )
        ).json() == []

        deleted = await client.delete(
            f"/api/v1/kb/{kb_id}",
            headers=_headers(owner_token),
        )
        assert deleted.status_code == 204, deleted.text
        assert OpenFgaTuple(
            f"organization:{owner['tenant_id']}",
            "organization",
            f"knowledge_base:{kb_id}",
        ) not in store.tuples
        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "manager",
            f"knowledge_base:{kb_id}",
        ) not in store.tuples
        # A direct editor tuple can remain until reconciler convergence, but
        # tenant-bound SQL intersection hides the deleted row immediately.
        assert (
            await client.get(
                "/api/v1/kb",
                headers=_headers(editor_token),
            )
        ).json() == []


@pytest.mark.asyncio
async def test_kb_index_worker_fails_closed_after_captured_user_revocation(
    pg_engine,
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants(tenant_id, name) "
                "VALUES (:tenant_id, 'worker auth')"
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO users(user_id, tenant_id, email)
                VALUES (:user_id, :tenant_id, :email)
                """
            ),
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "email": f"kb-worker-{uuid.uuid4().hex}@example.com",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO org_memberships(
                    membership_id, user_id, tenant_id, org_role, status
                ) VALUES (
                    gen_random_uuid(), :user_id, :tenant_id,
                    'member', 'active'
                )
                """
            ),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = KbRepo(session)
        kb = await repo.create_kb(
            tenant_id=tenant_id,
            user_id=user_id,
            name="Revoked worker source",
        )
        file_row = await repo.create_file(
            kb_id=kb.id,
            tenant_id=tenant_id,
            user_id=user_id,
            name="revoked.txt",
            parser_type="txt",
            mime_type="text/plain",
            file_size=7,
            content_hash="a" * 64,
            object_store_key="kb/revoked.txt",
        )
        file_id = file_row.id

    store = _RelationshipStore()
    monkeypatch.setattr(
        "vibecanvas_api.background_tasks.kb_indexer."
        "openfga_client_from_config",
        lambda: store,
    )

    await asyncio.to_thread(
        lambda: kb_index_file_task(
            **{
                "task_id": str(uuid.uuid4()),
                "tenant_id": str(tenant_id),
                "file_id": str(file_id),
                "user_id": str(user_id),
            }
        )
    )

    async with session_scope(tenant_id=str(tenant_id)) as session:
        persisted = await KbRepo(session).get_file(file_id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.error_message == (
            "Authorization no longer permits indexing."
        )
