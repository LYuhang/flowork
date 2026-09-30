"""Tenant Skill catalog, installation, inspection, and removal routes."""
from __future__ import annotations

import uuid
import re
from typing import Literal

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.deps import (
    AuthContext,
    current_user,
    tenant_db,
)
from vibecanvas_api.authorization.dependencies import (
    context_for_auth,
    get_authz_service,
    mutation_coordinator_for_request,
    principal_for_auth,
)
from vibecanvas_api.authorization.openfga_client import OpenFgaUnavailableError
from vibecanvas_api.authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    resource_root_edges,
)
from vibecanvas_api.authorization.service import (
    AuthzService,
    batch_resource_decisions,
)
from vibecanvas_api.authorization.types import (
    Action,
    AuthorizedResource,
    ConsistencyPreference,
    Decision,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.security.upload_scanner import require_clean_upload
from vibecanvas_api.schemas.access import (
    access_from_decision,
)
from vibecanvas_api.schemas.skills import (
    SkillCatalogInstall,
    SkillDetailOut,
    SkillDraftOut,
    SkillDraftSave,
    SkillOut,
    SkillRevisionDetailOut,
    SkillRevisionOut,
    SkillVersionCreate,
)
from vibecanvas_api.services.skill_catalog import (
    download_skill_bundle,
    read_skill_catalog_file,
    resolve_skill_catalog_item,
    search_skill_catalog,
)
from vibecanvas_api.services.skill_loader import parse_skill_md
from vibecanvas_api.services.resource_provenance import (
    ResourceProvenanceBuilder,
)
from vibecanvas_api.services.skill_bundle import (
    unpack_skill_zip, validate_skill_files,
)
from vibecanvas_api.storage.repo_skills import SkillsRepo


router = APIRouter(prefix="/api/v1/skills", tags=["skills"])
CatalogSource = Literal["openai", "anthropic"]


def _iso(value) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


async def _row_to_out(
    row: dict,
    decision: Decision,
    provenance: ResourceProvenanceBuilder,
    *,
    user_id: str,
) -> SkillOut:
    access = access_from_decision(decision)
    if row.get("source") != "custom" or str(row.get("user_id")) != str(user_id):
        access = access.model_copy(update={"capabilities": [
            action for action in access.capabilities if action not in {Action.UPDATE, Action.PUBLISH}
        ]})
    return SkillOut(
        id=str(row["skill_id"]),
        name=row["name"],
        description=row.get("description") or "",
        allowed_tools=list(row.get("allowed_tools") or []),
        version=row["version"],
        source=row.get("source"),
        source_id=row.get("source_id"),
        source_url=row.get("source_url"),
        source_revision=row.get("source_revision"),
        revision_hash=row.get("revision_hash"),
        created_at=_iso(row.get("created_at")),
        updated_at=_iso(row.get("updated_at")),
        access=access,
        provenance=await provenance.build(
            creator_user_id=row.get("user_id"),
            origin_type=(
                "created" if row.get("source") == "custom"
                else "catalog_install"
            ),
        ),
    )


def _require_owned_custom_skill(row: dict | None, user_id: str) -> None:
    """Editing is limited to the creator's custom package, even for admins."""
    if row is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if row.get("source") != "custom":
        raise HTTPException(status_code=403, detail={
            "code": "skill_read_only_source",
            "message": "Only custom Skills can be edited; installed catalog Skills are read-only.",
        })
    if str(row.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail={
            "code": "skill_not_creator",
            "message": "You can only edit custom Skills that you created.",
        })


def _bundle_paths(row: dict) -> list[str]:
    return sorted(
        str(item["path"])
        for item in (row.get("file_manifest") or [])
        if isinstance(item, dict) and item.get("path")
    )


def _replace_skill_md(
    files: list[tuple[str, str | None, bytes]], skill_md: str,
) -> list[tuple[str, str | None, bytes]]:
    updated = []
    found = False
    for path, content_type, data in files:
        if path == "SKILL.md":
            updated.append((path, "text/markdown", skill_md.encode("utf-8")))
            found = True
        else:
            updated.append((path, content_type, data))
    if not found:
        updated.append(("SKILL.md", "text/markdown", skill_md.encode("utf-8")))
    return updated


def _with_version(skill_md: str, version: int) -> str:
    """Update only the YAML version field while preserving author formatting."""
    stripped = skill_md.lstrip()
    leading = skill_md[: len(skill_md) - len(stripped)]
    end = stripped[3:].find("\n---")
    if not stripped.startswith("---") or end < 0:
        # Validation emits the canonical, more useful error below.
        return skill_md
    boundary = end + 3
    frontmatter = stripped[3:boundary]
    pattern = re.compile(r"(?m)^([ \t]*version[ \t]*:[ \t]*).*$")
    if pattern.search(frontmatter):
        frontmatter = pattern.sub(rf"\g<1>{version}", frontmatter, count=1)
    else:
        frontmatter = frontmatter.rstrip("\n") + f"\nversion: {version}\n"
    return leading + "---" + frontmatter + stripped[boundary:]


def _draft_out(
    *,
    skill_id: uuid.UUID,
    base_revision_hash: str,
    row: dict | None,
    skill_md: str,
    files: list[tuple[str, str | None, bytes]],
    decision: Decision,
    provenance,
) -> SkillDraftOut:
    body = parse_skill_md(skill_md)[1]
    return SkillDraftOut(
        skill_id=str(skill_id),
        base_revision_hash=base_revision_hash,
        draft_hash=row.get("draft_hash") if row else None,
        skill_md=skill_md,
        body=body,
        files=sorted(path for path, _content_type, _data in files),
        has_changes=row is not None,
        updated_at=_iso(row.get("updated_at")) if row else None,
        access=access_from_decision(decision),
        provenance=provenance,
    )


def _catalog_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (ValueError, UnicodeError)):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc) or "The Skill catalog did not respond in time",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Skill catalog request failed: {exc}",
    )


def _skill_resource(
    ctx: AuthContext,
    skill_id: uuid.UUID | str,
) -> ResourceRef:
    return ResourceRef(
        ResourceType.SKILL_INSTALLATION,
        str(skill_id),
        ctx.active_organization_id,
    )


def _skill_revision_resource(
    ctx: AuthContext,
    revision_id: uuid.UUID | str,
) -> ResourceRef:
    return ResourceRef(
        ResourceType.SKILL_REVISION,
        str(revision_id),
        ctx.active_organization_id,
    )


async def _authorize_skill(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    skill_id: uuid.UUID | str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = _skill_resource(ctx, skill_id)
    decision = await service.check(
        principal_for_auth(ctx),
        action,
        resource,
        context_for_auth(ctx, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="skill_not_found")
    return AuthorizedResource(resource=resource, decision=decision)


async def _authorize_skill_revision(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
    revision_id: uuid.UUID | str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = _skill_revision_resource(ctx, revision_id)
    decision = await service.check(
        principal_for_auth(ctx),
        action,
        resource,
        context_for_auth(ctx, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="skill_version_not_found")
    return AuthorizedResource(resource=resource, decision=decision)


async def _authorize_organization_create(
    *,
    request: Request,
    ctx: AuthContext,
    service: AuthzService,
) -> None:
    organization = ResourceRef(
        ResourceType.ORGANIZATION,
        ctx.active_organization_id,
        ctx.active_organization_id,
    )
    decision = await service.check(
        principal_for_auth(ctx),
        Action.CREATE,
        organization,
        context_for_auth(ctx, request),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="resource_not_found")


async def _rebind_request_organization(
    session: AsyncSession,
    ctx: AuthContext,
) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :organization_id, true)"),
        {"organization_id": ctx.active_organization_id},
    )


async def _finish_skill_creation(
    *,
    request: Request,
    ctx: AuthContext,
    session: AsyncSession,
    service: AuthzService,
    skill_id: uuid.UUID,
) -> Decision:
    coordinator = mutation_coordinator_for_request(
        request,
        ctx.active_organization_id,
    )
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=ctx.user_id,
        before=frozenset(),
        after=resource_root_edges(
            organization_id=ctx.active_organization_id,
            object_type="skill_installation",
            object_id=str(skill_id),
            owner_relation="manager",
            owner_type="user",
            owner_id=ctx.user_id,
        ),
        operation_id=uuid.uuid4().hex,
        source="skill-installation-create",
    )
    await session.commit()
    request.state.skill_publication_receipt = {"skill_id": str(skill_id), "published": True}
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_request_organization(session, ctx)
    decision = await service.check(
        principal_for_auth(ctx),
        Action.VIEW_METADATA,
        _skill_resource(ctx, skill_id),
        context_for_auth(
            ctx,
            request,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        ),
    )
    if not decision.allowed:
        raise OpenFgaUnavailableError(
            "authorization_projection_not_visible"
        )
    return decision


@router.get("")
async def list_skills(
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    principal = principal_for_auth(ctx)
    context = context_for_auth(ctx, request)
    authorized_ids = await service.list_authorized_ids(
        principal,
        Action.VIEW_METADATA,
        ResourceType.SKILL_INSTALLATION,
        context,
    )
    rows = await SkillsRepo(session).list_authorized(authorized_ids)
    resources = [
        _skill_resource(ctx, row["skill_id"])
        for row in rows
    ]
    decisions = await batch_resource_decisions(
        service,
        principal=principal,
        resources=resources,
        context=context,
    )
    provenance = ResourceProvenanceBuilder(session)
    return {
        "items": [
            await _row_to_out(row, decisions[resource], provenance, user_id=ctx.user_id)
            for row, resource in zip(rows, resources, strict=True)
        ]
    }


@router.get("/catalog")
async def catalog(
    source: CatalogSource,
    search: str = "",
    limit: int = Query(default=10, ge=1, le=100),
    ctx: AuthContext = Depends(current_user),
):
    try:
        return await search_skill_catalog(source=source, search=search, limit=limit)
    except (TimeoutError, httpx.HTTPError, ValueError, UnicodeError) as exc:
        raise _catalog_error(exc) from exc


@router.get("/catalog/resolve")
async def resolve_catalog_skill(
    source: CatalogSource,
    source_id: str,
    ctx: AuthContext = Depends(current_user),
):
    try:
        return await resolve_skill_catalog_item(source=source, source_id=source_id)
    except (TimeoutError, httpx.HTTPError, LookupError, ValueError, UnicodeError) as exc:
        raise _catalog_error(exc) from exc


@router.get("/catalog/file")
async def get_catalog_skill_file(
    source: CatalogSource,
    source_id: str,
    path: str,
    ctx: AuthContext = Depends(current_user),
):
    try:
        data, content_type = await read_skill_catalog_file(
            source=source, source_id=source_id, path=path
        )
    except (TimeoutError, httpx.HTTPError, LookupError, ValueError, UnicodeError) as exc:
        raise _catalog_error(exc) from exc
    return Response(content=data, media_type=content_type)


@router.post("/catalog/install", status_code=status.HTTP_201_CREATED, response_model=SkillOut)
async def install_catalog_skill(
    body: SkillCatalogInstall,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    repo = SkillsRepo(session)
    if await repo.find_live_source(
        source=body.source,
        source_id=body.source_id,
    ):
        raise HTTPException(status_code=409, detail="Skill is already installed")

    try:
        item, files = await download_skill_bundle(
            source=body.source,
            source_id=body.source_id,
        )
    except (httpx.HTTPError, LookupError, ValueError, UnicodeError) as exc:
        raise _catalog_error(exc) from exc

    tenant_id = uuid.UUID(ctx.tenant_id)
    skill_id = await repo.insert(
        tenant_id=tenant_id,
        user_id=uuid.UUID(ctx.user_id),
        name=item["name"],
        description=item["description"],
        version=item["version"],
        allowed_tools=item["allowed_tools"],
        source=item["source"],
        source_id=item["source_id"],
        source_url=item["homepage"],
        source_revision=item["revision"],
        files=files,
    )
    try:
        await session.flush()
    except IntegrityError as exc:
        repo.purge_bundle(tenant_id, skill_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Skill is already installed",
        ) from exc
    row = await repo.get(skill_id)
    decision = await _finish_skill_creation(
        request=request,
        ctx=ctx,
        session=session,
        service=service,
        skill_id=skill_id,
    )
    return await _row_to_out(
        row,
        decision,
        ResourceProvenanceBuilder(session),
        user_id=ctx.user_id,
    )


async def _read_custom_bundle(
    bundle: UploadFile,
) -> tuple[dict, list[tuple[str, str | None, bytes]]]:
    try:
        data = await bundle.read()
        await require_clean_upload(data)
        return unpack_skill_zip(data)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/custom", status_code=status.HTTP_201_CREATED, response_model=SkillOut)
async def create_custom_skill(
    request: Request,
    bundle: UploadFile = File(...),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_organization_create(
        request=request,
        ctx=ctx,
        service=service,
    )
    frontmatter, files = await _read_custom_bundle(bundle)
    repo = SkillsRepo(session)
    skill_id = await repo.insert(
        tenant_id=uuid.UUID(ctx.tenant_id),
        user_id=uuid.UUID(ctx.user_id),
        name=str(frontmatter["name"]).strip(),
        description=str(frontmatter["description"]).strip(),
        version=int(frontmatter.get("version") or 1),
        allowed_tools=list(frontmatter.get("allowed_tools") or []),
        source="custom",
        files=files,
    )
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"A Skill named {frontmatter['name']!r} already exists",
        ) from exc
    row = await repo.get(skill_id)
    decision = await _finish_skill_creation(
        request=request,
        ctx=ctx,
        session=session,
        service=service,
        skill_id=skill_id,
    )
    return await _row_to_out(
        row,
        decision,
        ResourceProvenanceBuilder(session),
        user_id=ctx.user_id,
    )


@router.get("/{skill_id}/draft", response_model=SkillDraftOut)
async def get_custom_skill_draft(
    skill_id: str,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    sid = _parse_uuid(skill_id)
    authorized = await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.VIEW,
    )
    repo = SkillsRepo(session)
    current = await repo.get(sid)
    if current is None or current.get("source") != "custom":
        raise HTTPException(status_code=404, detail="custom skill not found")
    draft = await repo.get_draft(sid)
    files = (
        await repo.read_draft_files(sid)
        if draft else await repo.read_current_files(sid)
    )
    files = files or []
    raw = next((data for path, _ct, data in files if path == "SKILL.md"), b"")
    try:
        skill_md = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=500, detail="Stored SKILL.md is not UTF-8") from exc
    return _draft_out(
        skill_id=sid,
        base_revision_hash=(
            draft["base_revision_hash"] if draft else current["revision_hash"]
        ),
        row=draft,
        skill_md=skill_md,
        files=files,
        decision=authorized.decision,
        provenance=await ResourceProvenanceBuilder(session).build(
            creator_user_id=current.get("user_id"),
            origin_type="created",
        ),
    )


@router.put("/{skill_id}/draft", response_model=SkillDraftOut)
async def save_custom_skill_draft(
    skill_id: str,
    body: SkillDraftSave,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    sid = _parse_uuid(skill_id)
    await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.UPDATE,
    )
    repo = SkillsRepo(session)
    current = await repo.get(sid)
    _require_owned_custom_skill(current, ctx.user_id)
    existing = await repo.read_draft_files(sid)
    if existing is None:
        existing = await repo.read_current_files(sid)
    try:
        _frontmatter, files = validate_skill_files(
            _replace_skill_md(existing or [], body.skill_md)
        )
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    authorized = await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    row = await repo.save_draft(
        skill_id=sid,
        tenant_id=uuid.UUID(ctx.tenant_id),
        files=files,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="custom skill not found")
    return _draft_out(
        skill_id=sid,
        base_revision_hash=row["base_revision_hash"],
        row=row,
        skill_md=body.skill_md,
        files=files,
        decision=authorized.decision,
        provenance=await ResourceProvenanceBuilder(session).build(
            creator_user_id=current.get("user_id"),
            origin_type="created",
        ),
    )


@router.post("/{skill_id}/versions", response_model=SkillOut)
async def publish_custom_skill_version(
    skill_id: str,
    body: SkillVersionCreate,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    sid = _parse_uuid(skill_id)
    await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.PUBLISH,
    )
    repo = SkillsRepo(session)
    current = await repo.get(sid)
    _require_owned_custom_skill(current, ctx.user_id)
    draft = await repo.get_draft(sid)
    files = await repo.read_draft_files(sid)
    if draft is None or files is None:
        raise HTTPException(status_code=409, detail="Save a draft before creating a version")
    raw = next((data for path, _ct, data in files if path == "SKILL.md"), b"")
    try:
        versioned_md = _with_version(raw.decode("utf-8"), body.version)
        frontmatter, versioned_files = validate_skill_files(
            _replace_skill_md(files, versioned_md)
        )
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    authorized = await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.PUBLISH,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    try:
        row = await repo.publish_draft(
            skill_id=sid,
            tenant_id=uuid.UUID(ctx.tenant_id),
            name=str(frontmatter["name"]).strip(),
            description=str(frontmatter["description"]).strip(),
            version=body.version,
            allowed_tools=list(frontmatter.get("allowed_tools") or []),
            expected_draft_hash=draft["draft_hash"],
            files=versioned_files,
        )
        await session.flush()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="The Skill name or version already exists",
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="custom skill not found")
    return await _row_to_out(
        row,
        authorized.decision,
        ResourceProvenanceBuilder(session),
        user_id=ctx.user_id,
    )


@router.put("/{skill_id}/bundle", response_model=SkillOut)
async def update_custom_skill_bundle(
    skill_id: str,
    request: Request,
    bundle: UploadFile = File(...),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    """Replace an owned custom package and publish its next version atomically."""
    sid = _parse_uuid(skill_id)
    for action in (Action.UPDATE, Action.PUBLISH):
        authorized = await _authorize_skill(
            request=request, ctx=ctx, service=service, skill_id=sid,
            action=action, consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        )
    repo = SkillsRepo(session)
    _require_owned_custom_skill(await repo.get(sid), ctx.user_id)
    _frontmatter, files = await _read_custom_bundle(bundle)
    # Serialize with UI publications before assigning the next version.
    current = await repo._lock_custom_skill(sid)
    _require_owned_custom_skill(current, ctx.user_id)
    version = int(current["version"]) + 1
    raw = next(data for path, _ct, data in files if path == "SKILL.md")
    try:
        frontmatter, files = validate_skill_files(_replace_skill_md(
            files, _with_version(raw.decode("utf-8"), version),
        ))
        draft = await repo.save_draft(
            skill_id=sid, tenant_id=uuid.UUID(ctx.tenant_id), files=files,
        )
        if draft is None:
            raise HTTPException(404, "custom skill not found")
        row = await repo.publish_draft(
            skill_id=sid, tenant_id=uuid.UUID(ctx.tenant_id), version=version,
            name=str(frontmatter["name"]).strip(),
            description=str(frontmatter["description"]).strip(),
            allowed_tools=list(frontmatter.get("allowed_tools") or []),
            expected_draft_hash=draft["draft_hash"], files=files,
        )
        await session.flush()
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, "The Skill name or version already exists") from exc
    if row is None:
        raise HTTPException(404, "custom skill not found")
    return await _row_to_out(row, authorized.decision, ResourceProvenanceBuilder(session), user_id=ctx.user_id)


@router.get("/{skill_id}/versions", response_model=list[SkillRevisionOut])
async def list_skill_versions(
    skill_id: str,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    sid = _parse_uuid(skill_id)
    authorized = await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.VIEW,
    )
    repo = SkillsRepo(session)
    current = await repo.get(sid)
    if current is None:
        raise HTTPException(status_code=404, detail="skill not found")
    rows = await repo.list_revisions(sid)
    provenance = await ResourceProvenanceBuilder(session).build(
        creator_user_id=current.get("user_id"),
        origin_type=(
            "created" if current.get("source") == "custom"
            else "catalog_install"
        ),
    )
    return [
        SkillRevisionOut(
            revision_id=str(row["revision_id"]),
            revision_hash=row["revision_hash"],
            version=row["version"],
            is_latest=bool(row["is_latest"]),
            files=_bundle_paths(row),
            size_bytes=int(row.get("size_bytes") or 0),
            created_at=_iso(row.get("created_at")),
            access=access_from_decision(authorized.decision),
            provenance=provenance,
        )
        for row in rows
    ]


@router.get(
    "/{skill_id}/versions/{revision_id}",
    response_model=SkillRevisionDetailOut,
)
async def get_skill_version(
    skill_id: str,
    revision_id: str,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    sid = _parse_uuid(skill_id)
    rid = _parse_revision_uuid(revision_id)
    authorized = await _authorize_skill_revision(
        request=request,
        ctx=ctx,
        service=service,
        revision_id=rid,
        action=Action.VIEW,
    )
    repo = SkillsRepo(session)
    row = await repo.get_revision(sid, rid)
    current = await repo.get(sid)
    files = await repo.read_revision_files(sid, rid)
    if row is None or files is None or current is None:
        raise HTTPException(status_code=404, detail="skill version not found")
    raw = next((data for path, _ct, data in files if path == "SKILL.md"), None)
    if raw is None:
        raise HTTPException(status_code=500, detail="Skill version has no SKILL.md")
    try:
        skill_md = raw.decode("utf-8")
        frontmatter, body = parse_skill_md(skill_md)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Invalid stored Skill: {exc}") from exc
    return SkillRevisionDetailOut(
        revision_id=str(row["revision_id"]),
        revision_hash=row["revision_hash"],
        version=row["version"],
        is_latest=bool(row["is_latest"]),
        files=sorted(path for path, _ct, _data in files),
        size_bytes=int(row.get("size_bytes") or 0),
        created_at=_iso(row.get("created_at")),
        name=str(frontmatter["name"]),
        description=str(frontmatter["description"]),
        allowed_tools=list(frontmatter.get("allowed_tools") or []),
        skill_md=skill_md,
        body=body,
        access=access_from_decision(authorized.decision),
        provenance=await ResourceProvenanceBuilder(session).build(
            creator_user_id=current.get("user_id"),
            origin_type=(
                "created" if current.get("source") == "custom"
                else "catalog_install"
            ),
        ),
    )


@router.get("/{skill_id}/versions/{revision_id}/files/{path:path}")
async def get_skill_version_file(
    skill_id: str,
    revision_id: str,
    path: str,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    sid = _parse_uuid(skill_id)
    rid = _parse_revision_uuid(revision_id)
    await _authorize_skill_revision(
        request=request,
        ctx=ctx,
        service=service,
        revision_id=rid,
        action=Action.VIEW,
    )
    if not path or ".." in path.split("/"):
        raise HTTPException(status_code=404, detail="skill file not found")
    files = await SkillsRepo(session).read_revision_files(
        sid, rid,
    )
    if files is None:
        raise HTTPException(status_code=404, detail="skill version not found")
    found = next(
        ((data, content_type) for item_path, content_type, data in files
         if item_path == path),
        None,
    )
    if found is None:
        raise HTTPException(status_code=404, detail="skill file not found")
    data, content_type = found
    return Response(
        content=data,
        media_type=content_type or "application/octet-stream",
    )


@router.get("/{skill_id}", response_model=SkillDetailOut)
async def get_skill(
    skill_id: str,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    repo = SkillsRepo(session)
    sid = _parse_uuid(skill_id)
    authorized = await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.VIEW,
    )
    row = await repo.get(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="skill not found")
    raw = await repo.read_bundle_file(sid, "SKILL.md")
    skill_md = raw.decode("utf-8") if raw is not None else ""
    body = parse_skill_md(skill_md)[1] if skill_md else ""
    base = await _row_to_out(
        row,
        authorized.decision,
        ResourceProvenanceBuilder(session),
        user_id=ctx.user_id,
    )
    files = _bundle_paths(row)
    draft = await repo.get_draft(sid)
    return SkillDetailOut(
        **base.model_dump(),
        body=body,
        skill_md=skill_md,
        files=files,
        has_draft=draft is not None,
        draft_updated_at=_iso(draft.get("updated_at")) if draft else None,
    )


@router.get("/{skill_id}/files/{path:path}")
async def get_skill_file(
    skill_id: str,
    path: str,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    sid = _parse_uuid(skill_id)
    if not path or ".." in path.split("/"):
        raise HTTPException(status_code=404, detail="skill file not found")
    await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.VIEW,
    )
    repo = SkillsRepo(session)
    if await repo.get(sid) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    result = await repo.read_bundle_file_with_type(sid, path)
    if result is None:
        raise HTTPException(status_code=404, detail="skill file not found")
    data, content_type = result
    return Response(content=data, media_type=content_type)


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: str,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    repo = SkillsRepo(session)
    sid = _parse_uuid(skill_id)
    await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.DELETE,
    )
    row = await repo.get(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="skill not found")
    await _authorize_skill(
        request=request,
        ctx=ctx,
        service=service,
        skill_id=sid,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    await repo.soft_delete(sid)
    coordinator = mutation_coordinator_for_request(
        request,
        ctx.active_organization_id,
    )
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=ctx.user_id,
        before=resource_root_edges(
            organization_id=ctx.active_organization_id,
            object_type="skill_installation",
            object_id=str(sid),
            owner_relation="manager",
            owner_type="user",
            owner_id=str(row["user_id"]),
        ),
        after=frozenset(),
        operation_id=uuid.uuid4().hex,
        source="skill-installation-delete",
    )
    await session.commit()
    request.state.skill_deletion_receipt = {"skill_id": str(skill_id), "deleted": True}
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _parse_uuid(skill_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="skill not found") from exc


def _parse_revision_uuid(revision_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="skill version not found") from exc
