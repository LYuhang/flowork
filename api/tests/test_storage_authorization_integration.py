"""Storage/VFS authorization roots, Workflow inheritance, and revoke."""

from __future__ import annotations

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
from vibecanvas_api.services.object_store import get_object_store


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
            return relation == "can_create_resource" and self._role(
                user,
                object_,
                {"owner", "admin", "member"},
            )
        if object_.startswith("workflow:"):
            content = {"viewer", "editor", "operator", "manager"}
            if relation == "can_view_metadata":
                return self._role(user, object_, content) or (
                    self._organization_role(
                        user,
                        object_,
                        {"owner", "admin", "auditor"},
                    )
                )
            if relation in {
                "can_view",
                "can_export",
                "can_use",
                "can_mount",
            }:
                return self._role(user, object_, content)
            if relation == "can_update":
                return self._role(user, object_, {"editor", "manager"})
            if relation in {
                "can_execute",
                "can_cancel",
                "can_inspect_runs",
            }:
                return self._role(user, object_, {"operator", "manager"})
            if relation in {
                "can_delete",
                "can_manage_access",
                "can_deploy",
            }:
                return self._role(user, object_, {"manager"}) or (
                    relation in {"can_delete", "can_manage_access"}
                    and self._organization_role(
                        user,
                        object_,
                        {"owner", "admin"},
                    )
                )
            return False
        if object_.startswith("storage_root:"):
            content = {"viewer", "editor", "operator", "manager"}
            if relation == "can_view_metadata":
                return self._role(user, object_, content) or (
                    self._organization_role(
                        user,
                        object_,
                        {"owner", "admin", "auditor"},
                    )
                )
            if relation == "can_view":
                return self._role(user, object_, content)
            if relation == "can_update":
                return self._role(user, object_, {"editor", "manager"})
            if relation == "can_mount":
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
        return False


from tests.test_workflow_authorization_integration import (
    _browser_sessions, _headers, _register,
)


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


async def _seed_workflow_file(
    app_engine,
    *,
    organization_id: str,
    workflow_id: str,
    path: str = "/data/shared.txt",
) -> None:
    data = b"shared workflow file"
    key = f"artifacts/{organization_id}/{workflow_id}{path}"
    get_object_store().put_bytes(key, data, "text/plain")
    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant, false)"),
            {"tenant": organization_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO vfs_artifacts(
                    scope_id, path, object_key, content_type, size_bytes
                ) VALUES (
                    :scope_id, :path, :object_key, 'text/plain', :size
                )
                """
            ),
            {
                "scope_id": workflow_id,
                "path": path,
                "object_key": key,
                "size": len(data),
            },
        )


@pytest.mark.asyncio
async def test_storage_root_and_workflow_vfs_authorization(
    app_engine,
    pg_engine,
    monkeypatch,
):
    monkeypatch.setattr(config, "resource_sharing_enabled", True)
    store = _RelationshipStore()
    app = build_app()
    app.state.openfga_client = store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        owner_token, owner = await _register(client, "storage_owner")
        viewer_token, viewer = await _register(client, "storage_viewer")
        outsider_token, outsider = await _register(
            client,
            "storage_outsider",
        )
        guest_token, guest = await _register(client, "storage_guest")
        auditor_token, auditor = await _register(client, "storage_auditor")
        admin_token, admin = await _register(client, "storage_admin")
        for member in (viewer, outsider):
            await _join_active_organization(
                pg_engine,
                user_id=member["user_id"],
                organization_id=owner["tenant_id"],
            )
        for role, member in (
            ("guest", guest),
            ("auditor", auditor),
            ("admin", admin),
        ):
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

        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "manager",
            f"storage_root:{owner['user_id']}",
        ) in store.tuples
        mount = await client.get(
            "/api/v1/storage/list",
            params={"path": "/mount"},
            headers=_headers(owner_token),
        )
        assert mount.status_code == 200, mount.text
        assert mount.json()["access"]["effective_role"] == "manager"
        assert "update" in mount.json()["access"]["capabilities"]

        created = await client.post(
            "/api/v1/workflows",
            json={"name": "Shared storage workflow"},
            headers=_headers(owner_token),
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["wf_id"]
        await _seed_workflow_file(
            app_engine,
            organization_id=owner["tenant_id"],
            workflow_id=workflow_id,
        )

        hidden = await client.get(
            "/api/v1/storage/list",
            params={"path": "/workflow"},
            headers=_headers(viewer_token),
        )
        assert hidden.status_code == 200
        assert hidden.json()["items"] == []
        guest_hidden = await client.get(
            "/api/v1/storage/list",
            params={"path": "/workflow"},
            headers=_headers(guest_token),
        )
        assert guest_hidden.status_code == 200
        assert guest_hidden.json()["items"] == []

        for token, is_admin in (
            (auditor_token, False),
            (admin_token, True),
        ):
            inventory = await client.get(
                "/api/v1/storage/list",
                params={"path": "/workflow"},
                headers=_headers(token),
            )
            assert inventory.status_code == 200, inventory.text
            item = inventory.json()["items"][0]
            assert item["name"] == workflow_id
            capabilities = set(item["access"]["capabilities"])
            assert "view_metadata" in capabilities
            assert "view" not in capabilities
            assert ("manage_access" in capabilities) is is_admin
            assert (
                await client.get(
                    "/api/v1/storage/content",
                    params={
                        "path": (
                            f"/workflow/{workflow_id}/data/shared.txt"
                        ),
                    },
                    headers=_headers(token),
                )
            ).status_code == 404

        resolved_viewer = await client.post(
            f"/api/v1/resource-access/workflow/{workflow_id}/resolve-target",
            json={
                "target_type": "user",
                "identifier": viewer["email"],
            },
            headers=_headers(owner_token),
        )
        assert resolved_viewer.status_code == 200, resolved_viewer.text
        viewer_target = resolved_viewer.json()["target"]
        assert viewer_target is not None
        granted = await client.post(
            f"/api/v1/workflows/{workflow_id}/access",
            json={
                "relation": "viewer",
                "resolution_token": viewer_target["resolution_token"],
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "storage-viewer-grant"},
            ),
        )
        assert granted.status_code == 201, granted.text

        resolved_guest = await client.post(
            f"/api/v1/resource-access/workflow/{workflow_id}/resolve-target",
            json={
                "target_type": "user",
                "identifier": guest["email"],
            },
            headers=_headers(owner_token),
        )
        assert resolved_guest.status_code == 200, resolved_guest.text
        guest_target = resolved_guest.json()["target"]
        assert guest_target is not None
        guest_granted = await client.post(
            f"/api/v1/workflows/{workflow_id}/access",
            json={
                "relation": "viewer",
                "resolution_token": guest_target["resolution_token"],
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "storage-guest-grant"},
            ),
        )
        assert guest_granted.status_code == 201, guest_granted.text

        visible = await client.get(
            "/api/v1/storage/list",
            params={"path": "/workflow"},
            headers=_headers(viewer_token),
        )
        assert [item["name"] for item in visible.json()["items"]] == [
            workflow_id
        ]
        assert (
            visible.json()["items"][0]["access"]["effective_role"]
            == "viewer"
        )

        storage_read = await client.get(
            "/api/v1/storage/content",
            params={
                "path": f"/workflow/{workflow_id}/data/shared.txt",
            },
            headers=_headers(viewer_token),
        )
        assert storage_read.status_code == 200, storage_read.text
        assert storage_read.json()["content"] == "shared workflow file"
        assert (
            storage_read.json()["access"]["effective_role"]
            == "viewer"
        )
        guest_read = await client.get(
            "/api/v1/storage/content",
            params={
                "path": f"/workflow/{workflow_id}/data/shared.txt",
            },
            headers=_headers(guest_token),
        )
        assert guest_read.status_code == 200, guest_read.text
        assert guest_read.json()["content"] == "shared workflow file"

        await _seed_workflow_file(pg_engine, organization_id=owner['tenant_id'],
                                  workflow_id=workflow_id, path='/chats/private-chat/notes.txt')
        for endpoint in ('/api/v1/vfs/content', '/api/v1/vfs/sign'):
            payload = {'wf_id': workflow_id, 'path': '/chats/private-chat/notes.txt'}
            response = await client.get(endpoint, params=payload, headers=_headers(viewer_token)) if endpoint.endswith('content') else await client.post(endpoint, json=payload, headers=_headers(viewer_token))
            assert response.status_code == 404, response.text

        vfs_list = await client.get(
            "/api/v1/vfs",
            params={"wf_id": workflow_id},
            headers=_headers(viewer_token),
        )
        assert vfs_list.status_code == 200, vfs_list.text
        assert [item["path"] for item in vfs_list.json()["entries"]] == [
            "/data/shared.txt"
        ]
        vfs_read = await client.get(
            "/api/v1/vfs/content",
            params={
                "wf_id": workflow_id,
                "path": "/data/shared.txt",
            },
            headers=_headers(viewer_token),
        )
        assert vfs_read.status_code == 200, vfs_read.text

        for token in (viewer_token, outsider_token):
            mutation = await client.put(
                "/api/v1/vfs/content",
                json={
                    "wf_id": workflow_id,
                    "path": "/data/blocked.txt",
                    "content": "blocked",
                    "content_type": "text/plain",
                },
                headers=_headers(token),
            )
            assert mutation.status_code == 404
        outsider_read = await client.get(
            "/api/v1/vfs/content",
            params={
                "wf_id": workflow_id,
                "path": "/data/shared.txt",
            },
            headers=_headers(outsider_token),
        )
        assert outsider_read.status_code == 404

        revoked = await client.request(
            "DELETE",
            f"/api/v1/workflows/{workflow_id}/access",
            json={
                "relation": "viewer",
                "subject_type": "user",
                "subject_id": viewer["user_id"],
                "subject_relation": None,
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "storage-viewer-revoke"},
            ),
        )
        assert revoked.status_code == 200, revoked.text
        guest_revoked = await client.request(
            "DELETE",
            f"/api/v1/workflows/{workflow_id}/access",
            json={
                "relation": "viewer",
                "subject_type": "user",
                "subject_id": guest["user_id"],
                "subject_relation": None,
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "storage-guest-revoke"},
            ),
        )
        assert guest_revoked.status_code == 200, guest_revoked.text
        after_revoke = await client.get(
            "/api/v1/storage/content",
            params={
                "path": f"/workflow/{workflow_id}/data/shared.txt",
            },
            headers=_headers(viewer_token),
        )
        assert after_revoke.status_code == 404
        after_revoke_vfs = await client.get(
            "/api/v1/vfs/content",
            params={
                "wf_id": workflow_id,
                "path": "/data/shared.txt",
            },
            headers=_headers(viewer_token),
        )
        assert after_revoke_vfs.status_code == 404
