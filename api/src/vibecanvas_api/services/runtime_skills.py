"""Resolve and materialize user VFS Skills for sandbox runtimes."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.authorization.service import AuthzService
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    ResourceType,
)
from vibecanvas_api.services.agent_runtime.protocol import RuntimeSkill
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_skills import SkillsRepo


def runtime_skill_scope(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode()).hexdigest()[:32]


def runtime_skill_root(chat_id: str, skill_id: str) -> str:
    return f"/skills/{runtime_skill_scope(chat_id)}/{skill_id}"


async def runtime_skill_descriptors(
    *,
    session: AsyncSession,
    chat_id: str,
    service: AuthzService,
    principal: PrincipalRef,
    context: AuthzRequestContext,
) -> list[RuntimeSkill]:
    """Return exactly the Skill installations this principal may ``use``."""
    authorized_ids = await service.list_authorized_ids(
        principal,
        Action.USE,
        ResourceType.SKILL_INSTALLATION,
        replace(context, consistency=ConsistencyPreference.HIGHER_CONSISTENCY),
    )
    rows = await SkillsRepo(session).list_authorized(authorized_ids)
    result = []
    for row in sorted(rows, key=lambda item: str(item["name"]).casefold()):
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


async def hydrate_runtime_skills(
    *,
    destination: str,
    tenant_id: str,
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

    async with session_scope(tenant_id=tenant_id) as session:
        skill_repo = SkillsRepo(session)
        rows = await skill_repo.list_authorized(tuple(requested))
        payloads = []
        for row in rows:
            skill_id = str(row["skill_id"])
            if str(row.get("revision_hash") or "") != requested.get(skill_id):
                # The immutable descriptor and the published HEAD no longer
                # agree. Do not mount a revision the host did not describe.
                continue
            files = await skill_repo.read_current_files(row["skill_id"])
            if files is None:
                continue
            for path, _content_type, data in files:
                relative = f"{skill_id}/{path}"
                payloads.append((relative, data))

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


async def refresh_runtime_skill(*, destination: str, tenant_id: str, skill_id: str, revision_id: str, revision_hash: str) -> int:
    """Replace one published runtime copy under a stable /skills mount root."""
    from vibecanvas_api.services.skill_bundle import validate_skill_files
    async with session_scope(tenant_id=tenant_id) as session:
        files = await SkillsRepo(session).read_revision_files(uuid.UUID(skill_id), uuid.UUID(revision_id))
        if files is None:
            raise LookupError('skill_version_unavailable')
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
