"""Collect obsolete immutable Skill versions without deleting live snapshots."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
from uuid import UUID

from vibecanvas_api.services.workflow_execution_authorization import _workflow_execution_is_active


async def reconcile_skill_cache(*, session, root: str, snapshot: dict | None = None, authorize=None) -> None:
    """Caller holds the mount's publication lock; database errors stop collection.

    Lease metadata lives outside the read-only mount. Every active execution
    pins its complete authorized view, including Skills not in its prompt.
    """
    root_path = Path(root)
    leases = root_path.parent / ("." + root_path.name + "-workflow-leases")
    key = None
    if snapshot is not None:
        claims = snapshot.get("execution")
        if not claims:
            return
        leases.mkdir(mode=0o700, exist_ok=True)
        import hashlib
        key = hashlib.sha256(json.dumps([claims, snapshot.get("lease_id")], sort_keys=True).encode()).hexdigest()
        current = {"execution": claims, "skills": [
            {"id": item["id"], "revision_hash": item["revision_hash"],
             "revision_id": item.get("revision_id")}
            for item in snapshot.get("skills", [])]}
        pending = leases / (key + ".tmp")
        pending.write_text(json.dumps(current), encoding="utf-8")
        os.replace(pending, leases / (key + ".json"))
    elif not leases.exists():
        return
    retained = set()
    expired = []
    for path in leases.glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        preparation = value['execution'].get('execution_resource_type') == 'deployment_preparation'
        if preparation:
            # The first invocation replaces the prewarmed snapshot with its
            # freshly resolved version. Idle preparation views are authorized
            # by the periodic callback below, without fabricating an invocation.
            active = snapshot is None or snapshot.get('execution', {}).get('execution_resource_type') == 'deployment_preparation'
        else:
            active = path.stem == key or await _workflow_execution_is_active(
                session, SimpleNamespace(**value["execution"]))
        if active:
            skills = value["skills"]
            if authorize is not None:
                skills = await authorize(value["execution"], skills)
            retained.update((str(UUID(item["id"])), item["revision_hash"])
                            for item in skills)
        else:
            expired.append(path)
    # Do not delete anything until every lease has been checked successfully.
    for installation in root_path.iterdir():
        if installation.is_symlink() or not installation.is_dir():
            continue
        try:
            identifier = str(UUID(installation.name))
        except ValueError:
            continue  # Chat-specific views are managed by their own lifecycle.
        for version in installation.iterdir():
            if (not version.is_symlink() and version.is_dir()
                    and re.fullmatch(r"[a-f0-9]{64}", version.name)
                    and (identifier, version.name) not in retained):
                shutil.rmtree(version)
        if not any(installation.iterdir()):
            installation.rmdir()
    for path in expired:
        path.unlink(missing_ok=True)


async def authorized_lease_skills(request, claims: dict, skills: list) -> list:
    """Recheck identity, execution, delegation and immutable revision access.

    Explicit denials remove files. Infrastructure errors propagate so a failed
    authorization lookup cannot be mistaken for a successful revocation check.
    """
    from fastapi import HTTPException
    from vibecanvas_api.authorization.types import Action, PrincipalType, ResourceRef, ResourceType
    from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution
    from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
    from vibecanvas_api.storage.repo_skills import SkillsRepo

    async def resolve(*, session, service, principal, authz_context, capability):
        delegated = None
        if principal.type == PrincipalType.SERVICE_ACCOUNT:
            delegated = set(await ServiceAccountsRepo(session).resource_refs(UUID(principal.id)))
        repo = SkillsRepo(session)
        allowed = []
        for item in skills:
            identifier = item["id"]
            if delegated is not None and ("skill_installation", identifier) not in delegated:
                continue
            row = await repo.get(identifier)
            if row is None:
                continue
            revision_id = item.get("revision_id")
            if not revision_id:
                # Older private leases recorded only the content hash.
                revisions = await repo.list_revisions(identifier)
                revision_id = next((str(r["revision_id"]) for r in revisions
                                    if r["revision_hash"] == item["revision_hash"]), None)
            if not revision_id or await repo.get_revision(identifier, revision_id) is None:
                continue
            decisions = [await service.check(principal, Action.USE,
                ResourceRef(kind, resource_id, capability.organization_id), authz_context)
                for kind, resource_id in ((ResourceType.SKILL_INSTALLATION, identifier),
                                           (ResourceType.SKILL_REVISION, revision_id))]
            if all(decision.allowed for decision in decisions):
                allowed.append(item)
        return allowed

    try:
        if claims.get('execution_resource_type') == 'deployment_preparation':
            from vibecanvas_api.services.deployment_resource_preflight import authorize_prepared_skills
            return await authorize_prepared_skills(request, claims, resolve=resolve)
        return await authorize_workflow_execution(request, SimpleNamespace(**claims), resolve=resolve)
    except HTTPException as exc:
        if exc.status_code in (401, 403, 409):
            return []
        raise
