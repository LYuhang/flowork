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


async def authorized_lease_skills(request, claims: dict, skills: list, *, include_files: bool = False) -> list:
    """Recheck identity, execution, delegation and immutable revision access.

    Explicit denials remove files. Infrastructure errors propagate so a failed
    authorization lookup cannot be mistaken for a successful revocation check.
    """
    from fastapi import HTTPException
    from vibecanvas_api.authorization.types import Action, ConsistencyPreference, PrincipalType, ResourceRef, ResourceType
    from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution
    from vibecanvas_api.storage.repo_skills import SkillsRepo

    async def resolve(*, session, service, principal, authz_context, capability):
        if principal.type in {PrincipalType.USER, PrincipalType.SERVICE_ACCOUNT}:
            from dataclasses import replace
            from sqlalchemy import text
            from vibecanvas_api.services.runtime_skills import authorized_skill_rows
            rows = await authorized_skill_rows(session=session, service=service, principal=principal, context=authz_context)
            by_id = {str(row["skill_id"]): row for row in rows}
            original = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one()
            allowed = []
            try:
                for item in skills:
                    row = by_id.get(item["id"])
                    if row is None or not item.get("revision_id"):
                        continue
                    owner = str(row["tenant_id"])
                    await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": owner})
                    scoped = replace(authz_context, consistency=ConsistencyPreference.HIGHER_CONSISTENCY, admitted_resource_organization_id=owner,
                        admitted_resource_type="skill_installation", admitted_resource_id=item["id"])
                    decisions = [await service.check(principal, Action.USE, ResourceRef(kind, identifier, owner), scoped)
                        for kind, identifier in ((ResourceType.SKILL_INSTALLATION, item["id"]),
                                                 (ResourceType.SKILL_REVISION, item["revision_id"]))]
                    if not all(decision.allowed for decision in decisions):
                        continue
                    repo = SkillsRepo(session)
                    revision = await repo.get_revision(item["id"], item["revision_id"])
                    if revision is None or revision["revision_hash"] != item["revision_hash"]:
                        continue
                    if include_files:
                        files = await repo.read_revision_files(UUID(item["id"]), UUID(item["revision_id"]))
                        if files is None:
                            continue
                        allowed.append({**item, "files": files})
                    else:
                        allowed.append(item)
            finally:
                await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": original or ""})
            return allowed
        return []

    try:
        if claims.get('execution_resource_type') == 'deployment_preparation':
            from vibecanvas_api.services.deployment_resource_preflight import authorize_prepared_skills
            return await authorize_prepared_skills(request, claims, resolve=resolve)
        return await authorize_workflow_execution(request, SimpleNamespace(**claims), resolve=resolve)
    except HTTPException as exc:
        if exc.status_code in (401, 403, 409):
            return []
        raise
