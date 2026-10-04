"""Resolve and materialize user VFS Skills for sandbox runtimes."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.authorization.service import AuthzService
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceType,
    ResourceRef,
)
from vibecanvas_api.services.agent_runtime.protocol import RuntimeSkill
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_skills import SkillsRepo


def runtime_skill_scope(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode()).hexdigest()[:32]


def runtime_skill_root(chat_id: str, skill_id: str) -> str:
    return f"/skills/{runtime_skill_scope(chat_id)}/{skill_id}"


async def prune_chat_skill_mounts(*, root: str, tenant_id: str, user_id: str) -> int:
    """Remove revoked/uninstalled packages from hashed Chat namespaces only.

    Workflow execution packages use a different directory shape and remain
    governed by their execution leases. The caller holds the sandbox lock.
    """
    from pathlib import Path
    from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
    from vibecanvas_api.storage.repo_skill_installations import SkillInstallationsRepo

    scopes = [entry for entry in Path(root).iterdir()
              if re.fullmatch(r'[0-9a-f]{32}', entry.name) and entry.is_dir() and not entry.is_symlink()]
    if not scopes:
        return 0
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        active = (await session.execute(text("""
            SELECT 1 FROM users u JOIN org_memberships m ON m.user_id=u.user_id
            WHERE u.user_id=:user AND u.status='active' AND m.tenant_id=:tenant AND m.status='active'
        """), {'user': uuid.UUID(user_id), 'tenant': uuid.UUID(tenant_id)})).first()
        installed = await SkillInstallationsRepo(session).installed_ids(uuid.UUID(user_id)) if active else set()
    allowed = set()
    if installed:
        client = openfga_client_from_config()
        try:
            from vibecanvas_api.services.skill_installation_cleanup import discard_inaccessible_installations
            retained = await discard_inaccessible_installations(client, user_id=user_id)
            allowed = {identifier for owner, identifier in retained if owner == user_id}
            from vibecanvas_api.storage.sync_session import short_admin_connection
            async with short_admin_connection() as connection:
                visible = (await connection.execute(text("""
                    SELECT s.skill_id FROM skills s
                    JOIN user_skill_installations i ON i.skill_id=s.skill_id AND i.user_id=:user
                    JOIN organizations owner ON owner.tenant_id=s.tenant_id
                    JOIN organizations workspace ON workspace.tenant_id=:workspace
                    WHERE owner.tenant_id=workspace.tenant_id OR
                          (owner.kind='personal' AND workspace.kind='personal')
                """), {'workspace': uuid.UUID(tenant_id), 'user': uuid.UUID(user_id)})).scalars().all()
            allowed.intersection_update(str(identifier) for identifier in visible)
        finally:
            await client.close()

    def remove():
        count = 0
        for scope in scopes:
            for entry in scope.iterdir():
                if entry.name in allowed:
                    continue
                if entry.is_symlink():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    continue
                count += 1
        return count
    return await asyncio.to_thread(remove)


async def authorized_skill_rows(*, session, service, principal, context):
    """Read local and explicitly shared packages using the same principal.

    Recipient projections only locate roots. Each foreign root is checked
    with higher consistency before its metadata is read under owner RLS.
    The request's original scope is restored even when a read fails.
    """
    context = replace(context, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    ids = await service.list_authorized_ids(principal, Action.USE, ResourceType.SKILL_INSTALLATION, context)
    rows = await SkillsRepo(session).list_authorized(ids)
    if principal.type == PrincipalType.SERVICE_ACCOUNT:
        from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
        delegated = set(await ServiceAccountsRepo(session).resource_refs(uuid.UUID(principal.id)))
        rows = [row for row in rows if ("skill_installation", str(row["skill_id"])) in delegated]
        return rows + await _shared_service_account_skills(
            session=session, service=service, principal=principal, context=context)
    from vibecanvas_api.storage.shared_resource_locator import shared_resource_roots
    roots = await shared_resource_roots(principal.id, active_organization_id=context.active_organization_id, resource_type="skill_installation")
    candidates = [(root.owner_tenant_id, root.resource_id) for root in roots
                  if str(root.owner_tenant_id) != context.active_organization_id]
    if not candidates:
        return rows
    original = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one()
    try:
        for owner, skill_id in candidates:
            owner_id = str(owner)
            await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": owner_id})
            scoped_context = replace(context, admitted_resource_organization_id=owner_id,
                admitted_resource_type="skill_installation", admitted_resource_id=skill_id)
            decision = await service.check(principal, Action.USE,
                ResourceRef(ResourceType.SKILL_INSTALLATION, skill_id, owner_id), scoped_context)
            if not decision.allowed:
                continue
            row = await SkillsRepo(session).get(uuid.UUID(skill_id))
            if row is not None and row.get("source") == "custom":
                rows.append(row)
    finally:
        await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": original or ""})
    return rows


async def _shared_service_account_skills(*, session, service, principal, context):
    """Foreign dependencies require both the bound account and its creator."""
    from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
    from vibecanvas_api.storage.models import User
    from vibecanvas_api.storage.models_org import OrgMembership
    accounts = ServiceAccountsRepo(session)
    account = await accounts.get(uuid.UUID(principal.id))
    if account is None or account.status != "active":
        return []
    owners = await accounts.resource_owners(account.service_account_id)
    foreign = [(identifier, owner) for (kind, identifier), owner in owners.items()
               if kind == "skill_installation" and owner != context.active_organization_id]
    if not foreign:
        return []
    membership = (await session.execute(select(OrgMembership).join(User, User.user_id == OrgMembership.user_id).where(
        OrgMembership.tenant_id == account.tenant_id,
        OrgMembership.user_id == account.created_by,
        OrgMembership.status == "active", User.status == "active",
    ))).scalar_one_or_none()
    if membership is None:
        return []
    creator = PrincipalRef(PrincipalType.USER, str(account.created_by))
    creator_context = AuthzRequestContext(active_organization_id=context.active_organization_id,
        membership_role=membership.org_role, membership_status=membership.status,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    original = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one() or ""
    rows = []
    try:
        for identifier, owner in foreign:
            await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": owner})
            root = ResourceRef(ResourceType.SKILL_INSTALLATION, identifier, owner)
            scope = dict(admitted_resource_organization_id=owner,
                         admitted_resource_type="skill_installation", admitted_resource_id=identifier)
            account_access = await service.check(principal, Action.USE, root, replace(context, **scope))
            creator_access = await service.check(creator, Action.USE, root, replace(creator_context, **scope))
            if not account_access.allowed or not creator_access.allowed:
                continue
            row = await SkillsRepo(session).get(uuid.UUID(identifier))
            if row is not None and row.get("source") == "custom":
                rows.append(row)
    finally:
        await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": original})
    return rows


async def runtime_skill_descriptors(
    *,
    session: AsyncSession,
    chat_id: str,
    service: AuthzService,
    principal: PrincipalRef,
    context: AuthzRequestContext,
) -> list[RuntimeSkill]:
    """Return exactly the Skill installations this principal may ``use``."""
    rows = await authorized_skill_rows(session=session, service=service, principal=principal, context=context)
    result = []
    for row in sorted(rows, key=lambda item: str(item["name"]).casefold()):
        if principal.type == PrincipalType.USER and not row.get("installed"):
            continue
        revision_hash = str(row.get("revision_hash") or "")
        if len(revision_hash) != 64:
            continue
        result.append(RuntimeSkill(
            skill_id=str(row["skill_id"]),
            name=str(row["name"]),
            description=str(row.get("description") or ""),
            revision_hash=revision_hash,
            root_path=runtime_skill_root(chat_id, str(row["skill_id"])),
            allowed_tools=list(row.get("allowed_tools") or []),
        ))
    return result


async def _authorized_runtime_packages(*, tenant_id: str, user_id: str, requested: dict[str, tuple[str, str | None]]):
    """Recheck the runtime user and load only the requested immutable revisions."""
    from vibecanvas_api.authorization.dependencies import authz_service_for_session
    from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
    from vibecanvas_api.storage.models import User
    from vibecanvas_api.storage.models_org import OrgMembership

    packages = {}
    client = openfga_client_from_config()
    try:
        async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
            membership = (await session.execute(select(OrgMembership).where(
                OrgMembership.tenant_id == uuid.UUID(tenant_id),
                OrgMembership.user_id == uuid.UUID(user_id),
                OrgMembership.status == "active",
            ))).scalar_one_or_none()
            user = await session.get(User, uuid.UUID(user_id))
            if membership is None or user is None or user.status != "active":
                raise PermissionError("runtime_skill_identity_inactive")
            principal = PrincipalRef(PrincipalType.USER, user_id)
            context = AuthzRequestContext(active_organization_id=tenant_id,
                membership_id=str(membership.membership_id), membership_role=membership.org_role,
                membership_status="active", consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
            service = authz_service_for_session(session=session, organization_id=tenant_id, openfga_client=client)
            rows = await authorized_skill_rows(session=session, service=service, principal=principal, context=context)
            for row in rows:
                if not row.get("installed"):
                    continue
                skill_id = str(row["skill_id"])
                expected = requested.get(skill_id)
                if expected is None or str(row.get("revision_hash") or "") != expected[0]:
                    continue
                if expected[1] is not None and str(row.get("current_revision_id")) != expected[1]:
                    continue
                owner = str(row["tenant_id"])
                await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": owner})
                scoped = replace(context, admitted_resource_organization_id=owner,
                    admitted_resource_type="skill_installation", admitted_resource_id=skill_id)
                decision = await service.check(principal, Action.USE,
                    ResourceRef(ResourceType.SKILL_INSTALLATION, skill_id, owner), scoped)
                if not decision.allowed:
                    continue
                files = await SkillsRepo(session).read_revision_files(row["skill_id"], row["current_revision_id"])
                if files is not None:
                    packages[skill_id] = files
    finally:
        await client.close()

    return packages


async def hydrate_runtime_skills(
    *,
    destination: str,
    tenant_id: str,
    user_id: str,
    skills: Sequence[RuntimeSkill | dict],
) -> int:
    """Project only published HEAD revisions into the Runtime's read-only view.

    Draft working trees and historical revisions remain durable in PostgreSQL
    but are intentionally absent from the sandbox mount. This makes the
    physical view match the descriptors sent in ``RuntimeTurnRequest.skills``.
    """
    requested: dict[str, str] = {}
    for item in skills:
        descriptor = (
            item if isinstance(item, RuntimeSkill)
            else RuntimeSkill.model_validate(item)
        )
        requested[descriptor.skill_id] = descriptor.revision_hash

    packages = await _authorized_runtime_packages(tenant_id=tenant_id, user_id=user_id,
        requested={skill_id: (revision_hash, None) for skill_id, revision_hash in requested.items()})
    payloads = [(f"{skill_id}/{path}", data) for skill_id, files in packages.items() for path, _, data in files]

    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)

    def _replace() -> int:
        staging = tempfile.mkdtemp(prefix=".skills-", dir=parent)
        try:
            for relative, data in payloads:
                target = os.path.join(staging, *relative.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as handle:
                    handle.write(data)
            previous = f"{destination}.old-{uuid.uuid4().hex}"
            if os.path.exists(destination):
                os.replace(destination, previous)
            os.replace(staging, destination)
            if os.path.exists(previous):
                shutil.rmtree(previous)
            return len(payloads)
        finally:
            if os.path.exists(staging):
                shutil.rmtree(staging)

    return await asyncio.to_thread(_replace)


async def refresh_runtime_skill(*, destination: str, tenant_id: str, user_id: str, skill_id: str, revision_id: str, revision_hash: str) -> int:
    """Replace one published runtime copy under a stable /skills mount root."""
    from vibecanvas_api.services.skill_bundle import validate_skill_files
    skill_id = str(uuid.UUID(skill_id))
    revision_id = str(uuid.UUID(revision_id))
    packages = await _authorized_runtime_packages(tenant_id=tenant_id, user_id=user_id,
        requested={skill_id: (revision_hash, revision_id)})
    files = packages.get(skill_id)
    if files is None:
        raise PermissionError("skill_version_unavailable_or_unauthorized")
    _, files = validate_skill_files(files)
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)

    def install():
        staging = tempfile.mkdtemp(prefix='.refresh-', dir=parent)
        backup = f'{destination}.old-{uuid.uuid4().hex}'
        try:
            for path, _, data in files:
                target = os.path.join(staging, *path.split('/'))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, 'wb') as handle:
                    handle.write(data)
            # The parent mount stays in place. A failed rename restores the
            # prior complete copy; obsolete package files disappear together.
            if os.path.exists(destination):
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
            except BaseException:
                if os.path.exists(backup):
                    os.replace(backup, destination)
                raise
            if os.path.exists(backup):
                shutil.rmtree(backup)
            return len(files)
        finally:
            if os.path.exists(staging):
                shutil.rmtree(staging)
    return await asyncio.to_thread(install)
