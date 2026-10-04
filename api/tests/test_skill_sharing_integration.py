"""Cross-personal Skill sharing through real HTTP, RLS and encrypted storage."""
import io
import zipfile
import pytest
from httpx import AsyncClient, ASGITransport

from vibecanvas_api.app import build_app
from vibecanvas_api.config import config
from tests.test_workflow_authorization_integration import (
    _RelationshipStore, _register_exact, _headers, _browser_sessions,
)


class SkillStore(_RelationshipStore):
    async def close(self):
        pass

    def _allowed(self, user, relation, object_):
        if not object_.startswith("skill_installation:"):
            return super()._allowed(user, relation, object_)
        if relation == "can_use" and self._has(user, "consumer", object_):
            return True
        roles = {
            "can_view_metadata": {"viewer", "editor", "manager"},
            "can_view": {"viewer", "editor", "manager"},
            "can_use": {"viewer", "editor", "manager"},
            "can_update": {"editor", "manager"},
            "can_publish": {"editor", "manager"},
            "can_delete": {"manager"},
            "can_manage_access": {"manager"},
        }
        return self._role(user, object_, roles.get(relation, set()))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "editor", "manager"])
async def test_shared_custom_skill_read_edit_publish_and_revoke(pg_engine, monkeypatch, role, tmp_path):
    monkeypatch.setattr(config, "resource_sharing_enabled", True)
    app = build_app()
    app.state.openfga_client = SkillStore()
    from vibecanvas_api.authorization import openfga_client
    monkeypatch.setattr(openfga_client, "openfga_client_from_config", lambda: app.state.openfga_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        owner_headers, owner, _ = await _register_exact(client, "skill_owner")
        recipient_headers, recipient, email = await _register_exact(client, "skill_recipient")
        package = io.BytesIO()
        content = "---\nname: shared-example\ndescription: Shared test package\nversion: 1\n---\nOriginal instruction.\n"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("SKILL.md", content)
            archive.writestr("references/example.txt", "shared file")
        created = await client.post("/api/v1/skills/custom", files={"bundle": ("skill.zip", package.getvalue(), "application/zip")}, headers=owner_headers)
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        assert (await client.get(f"/api/v1/skills/{sid}", headers=recipient_headers)).status_code == 404
        target = await client.post(f"/api/v1/resource-access/skill_installation/{sid}/resolve-target", json={"target_type": "user", "identifier": email}, headers=owner_headers)
        assert target.status_code == 200, target.text
        resolved = target.json()["target"]
        assert set(resolved["allowed_relations"]) == {"viewer", "editor", "manager"}
        granted = await client.post(f"/api/v1/skills/{sid}/access", json={"relation": role, "resolution_token": resolved["resolution_token"]}, headers=_headers(owner_headers, **{"Idempotency-Key": "share-skill"}))
        assert granted.status_code == 201, granted.text
        shared = await client.get("/api/v1/resource-access/shared?resource_type=skill_installation", headers=recipient_headers)
        assert shared.status_code == 200, shared.text
        assert any(item["resource_id"] == sid for item in shared.json()["items"])
        from uuid import UUID, uuid4
        from vibecanvas_api.services import service_account_resources as dependencies
        from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
        from vibecanvas_api.storage.db import session_scope
        monkeypatch.setattr(dependencies, "openfga_client_from_config", lambda: app.state.openfga_client)
        account_id = uuid4()
        dependency_graph = {"agent": {"node_type": "SubAgentNode", "node_config": {
            "skills": [{"id": sid, "name": "shared"}]}}}
        async with session_scope(tenant_id=recipient["active_organization_id"], user_id=recipient["user_id"]) as session:
            accounts = ServiceAccountsRepo(session)
            await accounts.create_for_owner(service_account_id=account_id, tenant_id=UUID(recipient["active_organization_id"]),
                name="Shared Skill task", kind="task", owner_resource_type="task",
                owner_resource_id=str(uuid4()), created_by=UUID(recipient["user_id"]))
        async def bind_dependency():
            async with session_scope(tenant_id=recipient["active_organization_id"], user_id=recipient["user_id"]) as session:
                await dependencies.bind_workflow_resources(session, tenant_id=UUID(recipient["active_organization_id"]),
                    service_account_id=account_id, created_by=recipient["user_id"], workflow=dependency_graph)
                return await ServiceAccountsRepo(session).resource_owners(account_id)
        assert await bind_dependency() == {("skill_installation", sid): owner["active_organization_id"]}
        from vibecanvas_api.services.runtime_skills import runtime_skill_descriptors
        from vibecanvas_api.authorization.dependencies import authz_service_for_session
        from vibecanvas_api.authorization.types import PrincipalRef, PrincipalType, AuthzRequestContext
        from vibecanvas_api.storage.db import session_scope
        async def discover():
            org = recipient["active_organization_id"]
            async with session_scope(tenant_id=org, user_id=recipient["user_id"]) as session:
                return await runtime_skill_descriptors(
                    session=session, chat_id="shared-skill-chat",
                    service=authz_service_for_session(session=session, organization_id=org, openfga_client=app.state.openfga_client),
                    principal=PrincipalRef(PrincipalType.USER, recipient["user_id"]),
                    context=AuthzRequestContext(active_organization_id=org, membership_role="owner", membership_status="active"),
                )
        from vibecanvas_api.services.workflow_resources import canonicalize_resource_names, resolve_workflow_resources
        async def canonical_name(*, snapshot=False, account=False):
            org = recipient["active_organization_id"]
            async with session_scope(tenant_id=org, user_id=recipient["user_id"]) as session:
                workflow = {"agent": {"node_type": "SubAgentNode", "node_config": {
                    "skills": [{"id": sid, "name": "unverified name"}]}}}
                resolve = resolve_workflow_resources if snapshot else canonicalize_resource_names
                canonical = await resolve(
                    session=session, workflow=workflow,
                    service=authz_service_for_session(session=session, organization_id=org, openfga_client=app.state.openfga_client),
                    principal=PrincipalRef(PrincipalType.SERVICE_ACCOUNT, str(account_id)) if account else PrincipalRef(PrincipalType.USER, recipient["user_id"]),
                    context=AuthzRequestContext(active_organization_id=org, membership_role="owner", membership_status="active"),
                )
                assert workflow["agent"]["node_config"]["skills"][0]["name"] == "unverified name"
                if snapshot:
                    return canonical
                return canonical["agent"]["node_config"]["skills"][0]["name"]
        assert await canonical_name() == created.json()["name"]
        from types import SimpleNamespace
        async def authorize_execution(request, capability, *, resolve):
            org = recipient["active_organization_id"]
            async with session_scope(tenant_id=org, user_id=recipient["user_id"]) as session:
                return await resolve(session=session,
                    service=authz_service_for_session(session=session, organization_id=org, openfga_client=app.state.openfga_client),
                    principal=PrincipalRef(PrincipalType.SERVICE_ACCOUNT, str(account_id)) if getattr(capability, "principal_type", "user") == "service_account" else PrincipalRef(PrincipalType.USER, recipient["user_id"]),
                    authz_context=AuthzRequestContext(active_organization_id=org, membership_role="owner", membership_status="active"),
                    capability=SimpleNamespace(organization_id=org))
        monkeypatch.setattr("vibecanvas_api.services.workflow_execution_authorization.authorize_workflow_execution", authorize_execution)
        from vibecanvas_api.services.workflow_resources import materialize_workflow_skills
        initial_snapshot = await canonical_name(snapshot=True)
        initial_snapshot["execution"] = {"execution_id": "sharing-fixture"}
        assert initial_snapshot["skills"][0]["id"] == sid
        from vibecanvas_api.authorization.mutations import AuthzMutationCoordinator, MutationEdge
        from vibecanvas_api.authorization.projection import enqueue_structural_delta, apply_committed_structural_mutations
        coordinator = AuthzMutationCoordinator(client=app.state.openfga_client, organization_id=recipient["active_organization_id"])
        async with session_scope(tenant_id=recipient["active_organization_id"], user_id=recipient["user_id"]) as session:
            mutations = await enqueue_structural_delta(session=session, coordinator=coordinator,
                actor_type="user", actor_id=recipient["user_id"], before=frozenset(),
                after={MutationEdge(owner["active_organization_id"], "skill_installation", sid, "consumer", "service_account", str(account_id))},
                operation_id=uuid4().hex, source="shared-dependency-test")
        await apply_committed_structural_mutations(coordinator, mutations)
        service_snapshot = await canonical_name(snapshot=True, account=True)
        assert service_snapshot["skills"][0]["id"] == sid
        service_snapshot["execution"] = {"execution_id": "service-sharing-fixture", "principal_type": "service_account"}
        await materialize_workflow_skills(root=str(tmp_path / "service-skills"), snapshot=service_snapshot)
        assert (tmp_path / "service-skills" / sid / service_snapshot["skills"][0]["revision_hash"] / "SKILL.md").is_file()
        async with session_scope(tenant_id=recipient["active_organization_id"]) as session:
            await ServiceAccountsRepo(session).set_status(account_id, status="disabled")
        with pytest.raises(PermissionError):
            await canonical_name(snapshot=True, account=True)
        async with session_scope(tenant_id=recipient["active_organization_id"]) as session:
            await ServiceAccountsRepo(session).set_status(account_id, status="active")


        assert all(item.skill_id != sid for item in await discover())
        assert created.json()["installed"] is True
        assert (await client.get(f"/api/v1/skills/{sid}", headers=recipient_headers)).json()["installed"] is False
        installation_url = f"/api/v1/skills/{sid}/installation"
        for _ in range(2):
            installed = await client.put(installation_url, headers=recipient_headers)
            assert installed.status_code == 204, installed.text
        assert any(item.skill_id == sid for item in await discover())
        assert (await client.get(f"/api/v1/skills/{sid}", headers=recipient_headers)).json()["installed"] is True
        from vibecanvas_api.services.runtime_skills import hydrate_runtime_skills
        installed_descriptors = await discover()
        stale_mount = tmp_path / "uninstall-mount" / ("a" * 32)
        assert await hydrate_runtime_skills(destination=str(stale_mount), tenant_id=recipient["active_organization_id"],
            user_id=recipient["user_id"], skills=installed_descriptors) == 2
        assert (stale_mount / sid / "SKILL.md").is_file()
        uninstalled = await client.delete(installation_url, headers=recipient_headers)
        assert uninstalled.status_code == 204, uninstalled.text
        assert all(item.skill_id != sid for item in await discover())
        from vibecanvas_api.services.runtime_skills import prune_chat_skill_mounts
        workflow_mount = stale_mount.parent / sid
        workflow_mount.mkdir()
        (workflow_mount / 'execution-package.txt').write_text('Retained execution dependency')
        removed = await prune_chat_skill_mounts(root=str(stale_mount.parent),
            tenant_id=recipient['active_organization_id'], user_id=recipient['user_id'])
        assert removed == 1
        assert not (stale_mount / sid).exists()
        assert (workflow_mount / 'execution-package.txt').is_file()
        # A pending turn can carry pre-uninstall descriptors. Materialization
        # must recheck the personal choice instead of trusting that snapshot.
        assert await hydrate_runtime_skills(destination=str(stale_mount), tenant_id=recipient["active_organization_id"],
            user_id=recipient["user_id"], skills=installed_descriptors) == 0
        assert not (stale_mount / sid).exists()
        assert (await client.get(f"/api/v1/skills/{sid}", headers=owner_headers)).json()["installed"] is True
        assert (await client.put(installation_url, headers=recipient_headers)).status_code == 204
        from vibecanvas_api.services.runtime_skills import hydrate_runtime_skills
        destination = tmp_path / "skills"
        descriptors = await discover()
        count = await hydrate_runtime_skills(destination=str(destination), tenant_id=recipient["active_organization_id"], user_id=recipient["user_id"], skills=descriptors)
        assert count == 2
        from types import SimpleNamespace
        from uuid import UUID
        from fastapi import HTTPException
        from vibecanvas_api.services.agent_runtime.cli_skills import download
        cli_context = SimpleNamespace(tenant_id=recipient["active_organization_id"], username=recipient["user_id"],
            turn_id="skill-sharing-test", chat_id="shared-skill-chat", authorization_client=app.state.openfga_client,
            authorization_membership_role="owner", authorization_membership_status="active")
        from vibecanvas_api.services.agent_runtime.cli_resources import read as read_resource
        discovered = await read_resource(cli_context, "skill.list", {"offset": 0, "limit": 20})
        assert discovered["status"] == "succeeded", discovered
        assert any(item["id"] == sid for item in discovered["items"])
        inspected = await read_resource(cli_context, "skill.get", {"skill_id": sid})
        assert inspected["status"] == "succeeded", inspected
        downloaded = await download(cli_context, UUID(sid))
        assert downloaded["file_count"] == 2
        assert (destination / sid / "references/example.txt").read_text() == "shared file"

        detail = await client.get(f"/api/v1/skills/{sid}", headers=recipient_headers)
        assert detail.status_code == 200, detail.text
        file = await client.get(f"/api/v1/skills/{sid}/draft/files/references/example.txt", headers=recipient_headers)
        assert file.status_code == 200, file.text
        assert file.text == "shared file"
        saved = await client.put(f"/api/v1/skills/{sid}/draft", json={"skill_md": content.replace("Original", "Updated"), "expected_hash": created.json()["revision_hash"]}, headers=recipient_headers)
        if role == "viewer":
            assert saved.status_code == 404, saved.text
        else:
            assert saved.status_code == 200, saved.text
            published = await client.post(f"/api/v1/skills/{sid}/versions", json={"version": 2, "expected_hash": saved.json()["draft_hash"]}, headers=recipient_headers)
            assert published.status_code == 200, published.text
            owner_detail = await client.get(f"/api/v1/skills/{sid}", headers=owner_headers)
            assert owner_detail.json()["version"] == 2
        from vibecanvas_api.services.runtime_skills import refresh_runtime_skill
        versions = await client.get(f"/api/v1/skills/{sid}/versions", headers=recipient_headers)
        assert versions.status_code == 200, versions.text
        latest = next(item for item in versions.json() if item["is_latest"])
        latest_snapshot = await canonical_name(snapshot=True)
        assert latest_snapshot["skills"][0]["revision_id"] == latest["revision_id"]
        immutable_root = tmp_path / "workflow-skills"
        await materialize_workflow_skills(root=str(immutable_root), snapshot=initial_snapshot)
        pinned_file = immutable_root / sid / initial_snapshot["skills"][0]["revision_hash"] / "SKILL.md"
        assert "Original" in pinned_file.read_text()

        if role != "viewer":
            assert initial_snapshot["skills"][0]["revision_id"] != latest["revision_id"]
        refresh_args = dict(destination=str(destination / sid), tenant_id=recipient["active_organization_id"],
            user_id=recipient["user_id"], skill_id=sid, revision_id=latest["revision_id"], revision_hash=latest["revision_hash"])
        assert await refresh_runtime_skill(**refresh_args) == 2
        assert ("Original" if role == "viewer" else "Updated") in (destination / sid / "SKILL.md").read_text()
        before = (destination / sid / "SKILL.md").read_bytes()
        with pytest.raises(PermissionError):
            await refresh_runtime_skill(**{**refresh_args, "revision_hash": "0" * 64})
        assert (destination / sid / "SKILL.md").read_bytes() == before
        descriptors = await discover()
        extra_role = 'editor' if role == 'viewer' else 'viewer'
        extra_target = await client.post(f"/api/v1/resource-access/skill_installation/{sid}/resolve-target",
            json={"target_type": "user", "identifier": email}, headers=owner_headers)
        extra = await client.post(f"/api/v1/skills/{sid}/access",
            json={"relation": extra_role, "resolution_token": extra_target.json()["target"]["resolution_token"]},
            headers=_headers(owner_headers, **{"Idempotency-Key": "additional-skill-role"}))
        assert extra.status_code == 201, extra.text
        removed_extra = await client.request('DELETE', f'/api/v1/skills/{sid}/access',
            json={'relation': extra_role, 'subject_type': 'user', 'subject_id': recipient['user_id']},
            headers=_headers(owner_headers, **{'Idempotency-Key': 'remove-additional-role'}))
        assert removed_extra.status_code == 200, removed_extra.text
        assert (await client.get(f'/api/v1/skills/{sid}', headers=recipient_headers)).json()['installed'] is True
        revoked = await client.request("DELETE", f"/api/v1/skills/{sid}/access", json={"relation": role, "subject_type": "user", "subject_id": recipient["user_id"]}, headers=_headers(owner_headers, **{"Idempotency-Key": "unshare-skill"}))
        assert revoked.status_code == 200, revoked.text
        with pytest.raises(HTTPException) as binding_denied:
            await bind_dependency()
        assert binding_denied.value.status_code == 403
        with pytest.raises(PermissionError):
            await refresh_runtime_skill(**refresh_args)
        assert all(item.skill_id != sid for item in await discover())
        assert await canonical_name() == "unverified name"
        with pytest.raises(PermissionError):
            await canonical_name(snapshot=True, account=True)
        with pytest.raises(PermissionError):
            await materialize_workflow_skills(root=str(tmp_path / "revoked-workflow-skills"), snapshot=initial_snapshot)
        assert not (tmp_path / "revoked-workflow-skills").exists()
        with pytest.raises(PermissionError):
            await materialize_workflow_skills(root=str(tmp_path / "revoked-service-skills"), snapshot=service_snapshot)
        assert not (tmp_path / "revoked-service-skills").exists()


        with pytest.raises(PermissionError):
            await canonical_name(snapshot=True)
        count = await hydrate_runtime_skills(destination=str(destination), tenant_id=recipient["active_organization_id"], user_id=recipient["user_id"], skills=descriptors)
        assert count == 0
        with pytest.raises(HTTPException) as denied:
            await download(cli_context, UUID(sid))
        assert denied.value.status_code == 404
        discovered = await read_resource(cli_context, "skill.list", {"offset": 0, "limit": 20})
        assert all(item["id"] != sid for item in discovered["items"])
        assert (await read_resource(cli_context, "skill.get", {"skill_id": sid}))["status"] == "failed"
        assert not (destination / sid).exists()
        assert (await client.get(f"/api/v1/skills/{sid}", headers=recipient_headers)).status_code == 404
        assert (await client.get(f"/api/v1/skills/{sid}/draft/files/references/example.txt", headers=recipient_headers)).status_code == 404
        target = await client.post(f"/api/v1/resource-access/skill_installation/{sid}/resolve-target",
            json={"target_type": "user", "identifier": email}, headers=owner_headers)
        granted = await client.post(f"/api/v1/skills/{sid}/access",
            json={"relation": role, "resolution_token": target.json()["target"]["resolution_token"]},
            headers=_headers(owner_headers, **{"Idempotency-Key": "reshare-uninstalled"}))
        assert granted.status_code == 201, granted.text
        assert (await client.get(f"/api/v1/skills/{sid}", headers=recipient_headers)).json()["installed"] is False
        assert all(item.skill_id != sid for item in await discover())
