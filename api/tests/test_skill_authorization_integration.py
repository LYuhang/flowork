"""Private Skill-installation authorization and Runtime inheritance."""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from vibecanvas_api.app import build_app
from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
)
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.authorization.types import (
    AuthzRequestContext,
    PrincipalRef,
    PrincipalType,
)
from vibecanvas_api.services.runtime_skills import (
    hydrate_runtime_skills,
    runtime_skill_descriptors,
)
from vibecanvas_api.storage.db import session_scope


SKILL_MD = (
    "---\n"
    "name: private-greet\n"
    "description: private greeting playbook\n"
    "allowed-tools: [bash]\n"
    "version: 1\n"
    "---\n"
    "# Playbook\n"
    "Say hello using the private reference."
)


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
            return (
                relation == "can_create_resource"
                and any(
                    self._has(user, role, object_)
                    for role in {"owner", "admin", "member"}
                )
            )
        if not object_.startswith("skill_installation:"):
            return False
        is_manager = self._has(user, "manager", object_)
        if relation == "can_view_metadata":
            return is_manager or self._organization_role(
                user,
                object_,
                {"owner", "admin", "auditor"},
            )
        if relation in {"can_view", "can_update", "can_use", "can_publish"}:
            return is_manager
        if relation == "can_delete":
            return is_manager or self._organization_role(
                user,
                object_,
                {"owner", "admin"},
            )
        return False


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def _bundle() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", SKILL_MD)
        archive.writestr("references/example.txt", "private reference")
    return output.getvalue()


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
    role: str,
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


@pytest.mark.asyncio
async def test_skill_installation_stays_private_and_owner_can_use_revisions(
    pg_engine,
    tmp_path,
):
    store = _RelationshipStore()
    app = build_app()
    app.state.openfga_client = store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        owner_token, owner = await _register(client, "skill_owner")
        outsider_token, outsider = await _register(client, "skill_outsider")
        guest_token, guest = await _register(client, "skill_guest")
        auditor_token, auditor = await _register(client, "skill_auditor")
        admin_token, admin = await _register(client, "skill_admin")
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

        guest_create = await client.post(
            "/api/v1/skills/custom",
            files={"bundle": ("guest.zip", _bundle(), "application/zip")},
            headers=_headers(guest_token),
        )
        assert guest_create.status_code == 404

        created = await client.post(
            "/api/v1/skills/custom",
            files={
                "bundle": (
                    "private-greet.zip",
                    _bundle(),
                    "application/zip",
                ),
            },
            headers=_headers(owner_token),
        )
        assert created.status_code == 201, created.text
        skill = created.json()
        skill_id = skill["id"]
        assert skill["access"]["effective_role"] == "manager"
        assert "manage_access" not in skill["access"]["capabilities"]
        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "manager",
            f"skill_installation:{skill_id}",
        ) in store.tuples

        for token, expected_capabilities in (
            (auditor_token, {"view_metadata"}),
            (admin_token, {"view_metadata", "delete"}),
        ):
            inventory = await client.get(
                "/api/v1/skills",
                headers=_headers(token),
            )
            assert inventory.status_code == 200, inventory.text
            item = inventory.json()["items"][0]
            assert item["id"] == skill_id
            assert set(item["access"]["capabilities"]) == (
                expected_capabilities
            )
            assert item["provenance"]["origin_type"] == "created"
            assert item["provenance"]["owner"]["display_name"]
            detail = await client.get(
                f"/api/v1/skills/{skill_id}",
                headers=_headers(token),
            )
            assert detail.status_code == 404

        assert (
            await client.get(
                "/api/v1/skills",
                headers=_headers(outsider_token),
            )
        ).json()["items"] == []
        assert (
            await client.get(
                f"/api/v1/skills/{skill_id}",
                headers=_headers(outsider_token),
            )
        ).status_code == 404

        for method in ("GET", "POST", "DELETE"):
            response = await client.request(
                method,
                f"/api/v1/skills/{skill_id}/access",
                json=(
                    {
                        "relation": "viewer",
                        "subject_type": "user",
                        "subject_id": outsider["user_id"],
                    }
                    if method != "GET"
                    else None
                ),
                headers=_headers(owner_token),
            )
            assert response.status_code == 404

        draft_md = SKILL_MD.replace(
            "Say hello using",
            "Say hello after reading",
        )
        saved = await client.put(
            f"/api/v1/skills/{skill_id}/draft",
            json={"skill_md": draft_md},
            headers=_headers(owner_token),
        )
        assert saved.status_code == 200, saved.text
        published = await client.post(
            f"/api/v1/skills/{skill_id}/versions",
            json={"version": 2},
            headers=_headers(owner_token),
        )
        assert published.status_code == 200, published.text

        async with session_scope(
            tenant_id=owner["tenant_id"],
        ) as session:
            service = authz_service_for_session(
                session=session,
                organization_id=owner["tenant_id"],
                openfga_client=store,
            )
            owner_skills = await runtime_skill_descriptors(
                chat_id="skill-authorization-test",
                session=session,
                service=service,
                principal=PrincipalRef(
                    PrincipalType.USER,
                    owner["user_id"],
                ),
                context=AuthzRequestContext(
                    active_organization_id=owner["tenant_id"],
                    membership_role="owner",
                    membership_status="active",
                ),
            )
        assert [item.skill_id for item in owner_skills] == [skill_id]

        runtime_root = tmp_path / "runtime-skills"
        hydrated = await hydrate_runtime_skills(
            destination=str(runtime_root),
            tenant_id=owner["tenant_id"],
            skills=owner_skills,
        )
        assert hydrated == 2
        assert "Say hello after reading" in (
            runtime_root / skill_id / "SKILL.md"
        ).read_text()

        deleted = await client.delete(
            f"/api/v1/skills/{skill_id}",
            headers=_headers(owner_token),
        )
        assert deleted.status_code == 204, deleted.text
        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "manager",
            f"skill_installation:{skill_id}",
        ) not in store.tuples
