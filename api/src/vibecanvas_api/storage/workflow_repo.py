# -*- coding: utf-8 -*-
"""
WorkflowRepo — unified workflow data access layer (Postgres backend).

The file/LRU implementation is replaced by a SQLAlchemy 2.0
async backend over the `workflows` / `workflow_versions` tables. Every
public method's return shape is byte-identical to the legacy filesystem
implementation so that ``routes/*`` use one durable repository contract.

Transaction model: methods call ``await self._s.flush()`` only. The caller
(route via ``get_db``/``session_scope``, or a test) owns ``commit()``.
Atomic sub-version allocation uses an INSERT whose ``sub`` is a
``MAX(sub)+1`` scalar subquery guarded by ``ON CONFLICT DO NOTHING`` on the
``(wf_id, major, sub)`` primary key, with a bounded retry — so concurrent
commits never collide or leave gaps.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.storage.models import Workflow, WorkflowVersion
from vibecanvas_api.security.content_encryption import content_encryption_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VersionPointer:
    sv: int
    parent_v: Optional[int]
    parent_sv: Optional[int]
    ts: float
    note: str = ""

    def to_dict(self) -> dict:
        return {"sv": self.sv, "parent_v": self.parent_v,
                "parent_sv": self.parent_sv, "ts": self.ts, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "VersionPointer":
        return cls(sv=d["sv"], parent_v=d.get("parent_v"),
                   parent_sv=d.get("parent_sv"), ts=d["ts"], note=d.get("note", ""))


# ---------------------------------------------------------------------------
# WorkflowRepo
# ---------------------------------------------------------------------------

class WorkflowRepo:
    """Tenant-bound workflow persistence; authorization stays in AuthzService."""

    def __init__(self, session: AsyncSession, user_id: str):
        self._s = session
        self._user_id = user_id

    async def _tenant_id(self) -> uuid.UUID:
        value = (
            await self._s.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            )
        ).scalar_one()
        if not value:
            raise RuntimeError("tenant context is required for Workflow metadata")
        return uuid.UUID(str(value))

    async def _metadata_storage_values(
        self,
        *,
        tenant_id: uuid.UUID,
        wf_id: str,
        workflow_name: str,
        description: str,
        tags: list,
    ) -> dict:
        encrypted = await content_encryption_service().encrypt_json(
            self._s,
            tenant_id=tenant_id,
            resource_type="organization_metadata",
            resource_id=str(tenant_id),
            purpose="workflow_metadata",
            record_id=wf_id,
            value={
                "workflow_name": workflow_name,
                "description": description,
                "tags": list(tags),
            },
        )
        return {
            "metadata_ciphertext": encrypted.ciphertext,
            "metadata_nonce": encrypted.nonce,
            "metadata_key_id": encrypted.key_id,
        }

    async def _materialize_metadata(self, workflow: Workflow) -> Workflow:
        if (
            workflow.metadata_key_id is None
            or not workflow.metadata_ciphertext
            or not workflow.metadata_nonce
        ):
            raise ValueError("workflow metadata ciphertext is missing")
        value = await content_encryption_service().decrypt_json(
            self._s,
            key_id=workflow.metadata_key_id,
            tenant_id=workflow.tenant_id,
            resource_type="organization_metadata",
            resource_id=str(workflow.tenant_id),
            purpose="workflow_metadata",
            record_id=workflow.wf_id,
            ciphertext=workflow.metadata_ciphertext,
            nonce=workflow.metadata_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("workflow metadata ciphertext must be an object")
        workflow.workflow_name = str(value.get("workflow_name") or "")
        workflow.description = str(value.get("description") or "")
        tags = value.get("tags")
        workflow.tags = list(tags) if isinstance(tags, list) else []
        return workflow

    async def _note_storage_values(
        self,
        *,
        workflow_row: Workflow,
        major: int,
        sub: int,
        note: str,
    ) -> dict:
        encrypted = await content_encryption_service().encrypt_json(
            self._s,
            tenant_id=workflow_row.tenant_id,
            resource_type="workflow",
            resource_id=workflow_row.wf_id,
            purpose="workflow_version_note",
            record_id=f"v{major}.sv{sub}",
            value={"note": note},
        )
        return {
            "note_ciphertext": encrypted.ciphertext,
            "note_nonce": encrypted.nonce,
            "note_key_id": encrypted.key_id,
        }

    async def _note_from_version(self, row: WorkflowVersion) -> str:
        if row.note_key_id is None or not row.note_ciphertext or not row.note_nonce:
            raise ValueError("workflow version note ciphertext is missing")
        value = await content_encryption_service().decrypt_json(
            self._s,
            key_id=row.note_key_id,
            tenant_id=row.tenant_id,
            resource_type="workflow",
            resource_id=row.wf_id,
            purpose="workflow_version_note",
            record_id=f"v{row.major}.sv{row.sub}",
            ciphertext=row.note_ciphertext,
            nonce=row.note_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("workflow version note ciphertext is invalid")
        row.note = str(value.get("note") or "")
        return row.note

    async def _workflow_storage_values(
        self,
        *,
        workflow_row: Workflow,
        workflow: dict,
        record_id: str,
    ) -> dict:
        encrypted = await content_encryption_service().encrypt_json(
            self._s,
            tenant_id=workflow_row.tenant_id,
            resource_type="workflow",
            resource_id=workflow_row.wf_id,
            purpose="workflow_version",
            record_id=record_id,
            value=workflow,
        )
        return {
            "workflow_ciphertext": encrypted.ciphertext,
            "workflow_nonce": encrypted.nonce,
            "workflow_key_id": encrypted.key_id,
        }

    async def _workflow_from_version(self, row: WorkflowVersion) -> dict:
        if (
            row.workflow_key_id is None
            or not row.workflow_ciphertext
            or not row.workflow_nonce
        ):
            raise ValueError("workflow version ciphertext is missing")
        value = await content_encryption_service().decrypt_json(
            self._s,
            key_id=row.workflow_key_id,
            tenant_id=row.tenant_id,
            resource_type="workflow",
            resource_id=row.wf_id,
            purpose="workflow_version",
            record_id=f"v{row.major}.sv{row.sub}",
            ciphertext=row.workflow_ciphertext,
            nonce=row.workflow_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("decrypted workflow version must be an object")
        return copy.deepcopy(value)

    async def _meta_to_dict(self, w: Workflow) -> dict:
        await self._materialize_metadata(w)
        return {
            "wf_id": w.wf_id, "workflow_name": w.workflow_name,
            "description": w.description, "creator": w.creator_user_id,
            "owner_id": w.owner_id,
            "domain": w.domain, "status": w.status,
            "active_v": w.active_major, "active_sv": w.active_sub,
            "active_major": w.active_major, "active_sub": w.active_sub,
            "tags": w.tags or [],
            "created_at": w.created_at.timestamp() if w.created_at else 0,
            "updated_at": w.updated_at.timestamp() if w.updated_at else 0,
        }

    async def create_workflow(self, wf_id: str = "", name: str = "",
                              description: str = "", creator_user_id: str = "",
                              domain: str = "public", status: str = "draft",
                              tags: list | None = None,
                              initial_workflow: dict | None = None,
                              initial_note: str = "init") -> dict:
        wf_id = wf_id or uuid.uuid4().hex[:12]
        owner_id = creator_user_id or self._user_id
        tenant_id = await self._tenant_id()
        metadata_values = await self._metadata_storage_values(
            tenant_id=tenant_id,
            wf_id=wf_id,
            workflow_name=name,
            description=description,
            tags=tags or [],
        )
        w = Workflow(
            wf_id=wf_id, tenant_id=tenant_id,
            creator_user_id=owner_id, owner_id=owner_id,
            domain=domain, status=status, active_major=1, active_sub=0,
            **metadata_values,
        )
        self._s.add(w)
        await self._s.flush()
        # Product creates use the default empty sv0. Importers and trusted
        # internal callers may atomically provide initial content while still
        # going through the same strict ciphertext boundary.
        storage_values = await self._workflow_storage_values(
            workflow_row=w,
            workflow=initial_workflow or {},
            record_id="v1.sv0",
        )
        note_values = await self._note_storage_values(
            workflow_row=w,
            major=1,
            sub=0,
            note=initial_note,
        )
        self._s.add(WorkflowVersion(
            wf_id=wf_id, major=1, sub=0, parent_major=None, parent_sub=None,
            **note_values, **storage_values,
            creator_user_id=creator_user_id or self._user_id))
        await self._s.flush()
        return await self._meta_to_dict(w)

    async def duplicate_workflow(self, wf_id: str,
                                 name: str | None = None) -> dict:
        """Copy an existing workflow's current graph into a brand-new
        workflow (fresh ``wf_id``, version history reset to v1.sv0).

        Returns the new workflow's meta dict, or ``{}`` if the source does
        not exist / is soft-deleted. RLS-scoped via the session — only the
        caller's own workflows are visible to ``get_meta`` /
        ``get_current_workflow`` so a foreign-tenant ``wf_id`` reads as
        missing here, which is correct.
        """
        src = await self.get_meta(wf_id)
        if not src:
            return {}
        graph = await self.get_current_workflow(wf_id)
        new_name = name or f"{src.get('workflow_name', '')} (copy)"
        new_wf_id = uuid.uuid4().hex[:12]
        tenant_id = await self._tenant_id()
        metadata_values = await self._metadata_storage_values(
            tenant_id=tenant_id,
            wf_id=new_wf_id,
            workflow_name=new_name,
            description=src.get("description", ""),
            tags=list(src.get("tags") or []),
        )
        w = Workflow(
            wf_id=new_wf_id, tenant_id=tenant_id,
            creator_user_id=self._user_id, owner_id=self._user_id,
            domain=src.get("domain", "public"), status="draft",
            active_major=1, active_sub=0, **metadata_values,
        )
        self._s.add(w)
        await self._s.flush()
        storage_values = await self._workflow_storage_values(
            workflow_row=w,
            workflow=copy.deepcopy(graph),
            record_id="v1.sv0",
        )
        note_values = await self._note_storage_values(
            workflow_row=w,
            major=1,
            sub=0,
            note=f"duplicated from {wf_id}",
        )
        self._s.add(WorkflowVersion(
            wf_id=new_wf_id, major=1, sub=0, parent_major=None,
            parent_sub=None, **storage_values,
            **note_values, creator_user_id=self._user_id))
        await self._s.flush()
        return await self._meta_to_dict(w)

    async def get_meta(self, wf_id: str, *, for_update: bool = False) -> dict:
        # Version operations must refresh any instance loaded before waiting
        # for the lock; otherwise content and parent pointers can diverge.
        options = {"with_for_update": True, "populate_existing": True} if for_update else {}
        w = await self._s.get(Workflow, wf_id, **options)
        if not w or w.deleted_at is not None:
            return {}
        return await self._meta_to_dict(w)

    # reload_meta is identical to get_meta under Postgres (no cache layer)
    reload_meta = get_meta

    async def get_current_workflow(self, wf_id: str) -> dict:
        w = await self._s.get(Workflow, wf_id)
        if not w or w.deleted_at is not None:
            return {}
        return await self.get_workflow_at(wf_id, w.active_major, w.active_sub)

    async def get_workflow_at(self, wf_id: str, v: int, sv: int) -> dict:
        row = await self._s.get(WorkflowVersion, (wf_id, v, sv))
        return await self._workflow_from_version(row) if row else {}

    async def commit(self, wf_id: str, workflow: dict, note: str = "",
                     editor: str = "",
                     target_major: int | None = None,
                     stamp_metadata: bool = False) -> "VersionPointer":
        """Allocate a sub-version atomically with bounded conflict retries.

        ``target_major`` (editable-historical-versions, UX-5):
          - ``None`` (default) → commit to the ACTIVE major and advance its
            ``active_sub`` HEAD pointer. This is the long-standing behaviour;
            every existing caller is byte-for-byte unaffected.
          - given ``m`` → commit a new sub UNDER major ``m`` (``max(sub)+1``
            for that major), then move HEAD to ``(m, new_sub)``. This is the
            "load a historical version → edit → Save lands under it"
            (git checkout-then-commit) semantics. ``m`` must already have at
            least one row for this workflow, else ``ValueError`` (404-ish).

        The durable commit happens in the FastAPI
        dependency teardown (``get_db``), AFTER any in-process asyncio
        lock the route held has released — so the asyncio lock never
        covered the durable write and two concurrent commits could
        regress the ``active_sub`` HEAD pointer under READ COMMITTED.
        We instead serialize per-``wf_id`` head mutations at the DB:
        ``SELECT ... FOR UPDATE`` on the ``Workflow`` row. Postgres
        holds that row lock until THIS transaction commits at teardown,
        so it genuinely covers the durable write. The ON CONFLICT +
        retry block below is kept verbatim as defense-in-depth (with
        the row lock it simply won't contend).
        """
        w = await self._s.get(Workflow, wf_id, with_for_update=True, populate_existing=True)
        if not w or w.deleted_at is not None:
            raise ValueError(f"Workflow {wf_id} not found")
        if target_major is None:
            major = w.active_major
        else:
            # Targeting an explicit major (historical-version Save). It must
            # already exist for this workflow — otherwise we'd silently create
            # a brand-new major out of a stale URL, which is never the intent.
            exists = (await self._s.execute(
                select(func.count())
                .select_from(WorkflowVersion)
                .where(WorkflowVersion.wf_id == wf_id,
                       WorkflowVersion.major == target_major)
            )).scalar_one()
            if not exists:
                raise ValueError(
                    f"major v{target_major} not found for workflow {wf_id}")
            major = target_major
        # parent_sub is the sub this commit descends from: for an active commit
        # that's the current HEAD sub; for a targeted historical commit it's
        # the latest sub of THAT major (we branch off its tip).
        parent_sub = w.active_sub if target_major is None else (
            await self.max_subversion(wf_id, major))
        for _ in range(5):
            next_sub = int((await self._s.execute(
                select(func.coalesce(func.max(WorkflowVersion.sub), -1) + 1)
                .where(WorkflowVersion.wf_id == wf_id,
                       WorkflowVersion.major == major)
            )).scalar_one())
            if stamp_metadata:
                workflow = copy.deepcopy(workflow)
                current_meta = await self._meta_to_dict(w)
                workflow.setdefault("__meta__", {}).update(
                    workflow_id=wf_id, workflow_name=current_meta.get("workflow_name", ""),
                    workflow_version=major, workflow_subversion=next_sub,
                )
            storage_values = await self._workflow_storage_values(
                workflow_row=w,
                workflow=workflow,
                record_id=f"v{major}.sv{next_sub}",
            )
            note_values = await self._note_storage_values(
                workflow_row=w,
                major=major,
                sub=next_sub,
                note=note,
            )
            stmt = (
                pg_insert(WorkflowVersion)
                .values(
                    wf_id=wf_id, major=major, sub=next_sub,
                    parent_major=major, parent_sub=parent_sub,
                    **note_values, **storage_values,
                    creator_user_id=editor or self._user_id)
                .on_conflict_do_nothing(
                    index_elements=["wf_id", "major", "sub"])
                .returning(WorkflowVersion.sub)
            )
            res = (await self._s.execute(stmt)).scalar_one_or_none()
            if res is not None:
                await self._s.execute(
                    update(Workflow).where(Workflow.wf_id == wf_id)
                    .values(active_major=major, active_sub=res))
                await self._s.flush()
                return VersionPointer(sv=res, parent_v=major,
                                      parent_sv=parent_sub, ts=_now().timestamp(),
                                      note=note)
        raise RuntimeError(f"sv allocation failed after 5 retries for {wf_id}")

    async def new_version(self, wf_id: str, workflow: dict,
                          note: str = "New Major Version", *,
                          stamp_metadata: bool = False,
                          source_version: tuple[int, int] | None = None) -> int:
        """Create a new major version (sub=0) and move HEAD to it.

        Legacy return shape: the new major version number (``int``).

        ``source_version`` records a Chat-selected parent rather than global
        HEAD. Callers supplying a snapshot must lock the workflow BEFORE
        reading it and hold that lock through this method and transaction commit.

        Row-lock the ``Workflow`` row with ``FOR UPDATE``
        so the ``MAX(major)+1`` read-modify-write is race-free per
        ``wf_id`` and the HEAD-pointer move is serialized at the DB
        (the asyncio lock did not cover the teardown commit).
        """
        w = await self._s.get(Workflow, wf_id, with_for_update=True, populate_existing=True)
        if not w or w.deleted_at is not None:
            raise ValueError(f"Workflow {wf_id} not found")
        cur_major, cur_sub = source_version or (w.active_major, w.active_sub)
        if source_version is not None and await self._s.get(WorkflowVersion, (wf_id, cur_major, cur_sub)) is None:
            raise ValueError("The source workflow version does not exist")
        max_major = (await self._s.execute(
            select(func.coalesce(func.max(WorkflowVersion.major), 0))
            .where(WorkflowVersion.wf_id == wf_id)
        )).scalar_one()
        new_v = (max_major + 1) if max_major else 1
        if stamp_metadata:
            workflow = copy.deepcopy(workflow)
            current_meta = await self._meta_to_dict(w)
            workflow.setdefault("__meta__", {}).update(
                workflow_id=wf_id, workflow_name=current_meta.get("workflow_name", ""),
                workflow_version=new_v, workflow_subversion=0,
            )
        storage_values = await self._workflow_storage_values(
            workflow_row=w,
            workflow=workflow,
            record_id=f"v{new_v}.sv0",
        )
        note_values = await self._note_storage_values(
            workflow_row=w,
            major=new_v,
            sub=0,
            note=note,
        )
        self._s.add(WorkflowVersion(
            wf_id=wf_id, major=new_v, sub=0,
            parent_major=cur_major, parent_sub=cur_sub,
            **note_values, **storage_values,
            creator_user_id=self._user_id))
        await self._s.execute(
            update(Workflow).where(Workflow.wf_id == wf_id)
            .values(active_major=new_v, active_sub=0))
        await self._s.flush()
        return new_v

    # ===================================================================
    # Lifecycle API
    # ===================================================================

    async def delete_workflow(self, wf_id: str) -> bool:
        """Soft delete. Legacy return shape: ``bool`` (False if not found
        or already deleted, True on success)."""
        res = await self._s.execute(
            update(Workflow)
            .where(Workflow.wf_id == wf_id, Workflow.deleted_at.is_(None))
            .values(deleted_at=_now())
        )
        await self._s.flush()
        return res.rowcount > 0

    async def list_authorized_workflows(
        self,
        workflow_ids: tuple[str, ...] | list[str],
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        """Intersect opaque AuthzService ids with this RLS-bound tenant.

        OpenFGA decides relationship visibility while PostgreSQL remains the
        organization boundary and source of metadata.  Keeping the
        intersection in SQL also prevents an unbounded in-memory load before
        pagination.
        """
        unique_ids = tuple(dict.fromkeys(str(value) for value in workflow_ids))
        if not unique_ids:
            return [], 0
        predicate = (
            Workflow.wf_id.in_(unique_ids),
            Workflow.deleted_at.is_(None),
        )
        total = (
            await self._s.execute(
                select(func.count()).select_from(Workflow).where(*predicate)
            )
        ).scalar_one()
        rows = (
            await self._s.execute(
                select(Workflow)
                .where(*predicate)
                .order_by(Workflow.updated_at.desc(), Workflow.wf_id)
                .offset(max(0, offset))
                .limit(max(1, limit))
            )
        ).scalars().all()
        items = []
        for workflow in rows:
            items.append(await self._meta_to_dict(workflow))
        return items, int(total)

    async def update_meta(self, wf_id: str, **fields) -> dict:
        """Update provided meta fields. Legacy return shape: updated meta
        dict, or ``{}`` if the workflow does not exist.

        The DB trigger owns ``updated_at`` — never set it manually.
        """
        # Metadata is encrypted as one object. Serialize read/merge/write for
        # all callers (HTTP and CLI), refreshing any previously loaded row.
        w = (await self._s.execute(
            select(Workflow).where(
                Workflow.wf_id == wf_id, Workflow.deleted_at.is_(None),
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if w is None:
            return {}
        allowed = {
            "workflow_name", "description", "domain",
            "status", "tags",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if clean:
            await self._materialize_metadata(w)
            private = {
                "workflow_name": w.workflow_name,
                "description": w.description,
                "tags": list(w.tags or []),
            }
            private.update({
                key: value
                for key, value in clean.items()
                if key in {"workflow_name", "description", "tags"}
            })
            metadata_values = await self._metadata_storage_values(
                tenant_id=w.tenant_id,
                wf_id=w.wf_id,
                workflow_name=str(private["workflow_name"] or ""),
                description=str(private["description"] or ""),
                tags=list(private["tags"] or []),
            )
            structural = {
                key: value
                for key, value in clean.items()
                if key in {"domain", "status"}
            }
            await self._s.execute(
                update(Workflow)
                .where(Workflow.wf_id == wf_id)
                .values(**metadata_values, **structural)
            )
        await self._s.flush()
        # Re-query: the prior instance may be expired after flush/commit.
        w = await self._s.get(Workflow, wf_id)
        return await self._meta_to_dict(w) if w else {}

    # ===================================================================
    # Version queries
    # ===================================================================

    async def list_major_versions(self, wf_id: str) -> list[dict]:
        """Legacy return shape: ``[{"v", "sv", "label"}]`` ordered by v."""
        rows = (await self._s.execute(
            select(WorkflowVersion.major,
                   func.max(WorkflowVersion.sub))
            .where(WorkflowVersion.wf_id == wf_id)
            .group_by(WorkflowVersion.major)
            .order_by(WorkflowVersion.major)
        )).all()
        return [{"v": v, "sv": max_sv, "label": f"v{v}.{max_sv}"}
                for v, max_sv in rows]

    async def checkout_major(self, wf_id: str, major_version: int) -> dict:
        """Switch HEAD to the latest sub of ``major_version``.

        Legacy return shape: that version's workflow content dict, or
        ``{}`` if the major version has no rows.

        Row-lock the ``Workflow`` row with ``FOR UPDATE``
        before the read-modify-write so the HEAD-pointer move is
        serialized at the DB across the dependency-teardown commit
        (the asyncio lock did not cover it). Missing/deleted workflows or
        missing majors return an empty dict. The stateless CLI has no checkout command; graph writes target an
        explicit major and do not create a Chat pointer.
        """
        w = await self._s.get(Workflow, wf_id, with_for_update=True, populate_existing=True)
        if not w or w.deleted_at is not None:
            return {}
        max_sv = (await self._s.execute(
            select(func.max(WorkflowVersion.sub))
            .where(WorkflowVersion.wf_id == wf_id,
                   WorkflowVersion.major == major_version)
        )).scalar_one_or_none()
        if max_sv is None:
            return {}
        await self._s.execute(
            update(Workflow).where(Workflow.wf_id == wf_id)
            .values(active_major=major_version, active_sub=max_sv)
        )
        await self._s.flush()
        return await self.get_workflow_at(wf_id, major_version, max_sv)

    async def get_version_history(self, wf_id: str) -> list[dict]:
        """Full version history across all major versions, ordered by
        (major, sub).

        Legacy return shape preserved verbatim (``version_id``,
        ``version_str``, ``v``, ``sv``, ``message``, ``timestamp``);
        the Postgres-era keys (``major``, ``sub``, ``parent_major``,
        ``parent_sub``, ``note``, ``ts``) are added as a superset (the
        route passes this list through untyped, so extra keys are safe).
        """
        rows = (await self._s.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.wf_id == wf_id)
            .order_by(WorkflowVersion.major, WorkflowVersion.sub)
        )).scalars().all()
        result = []
        for r in rows:
            await self._note_from_version(r)
            ts = r.ts.timestamp() if r.ts else 0
            result.append({
                "version_id": f"{wf_id}_v{r.major}_sv{r.sub}",
                "version_str": f"v{r.major}.{r.sub}",
                "v": r.major, "sv": r.sub,
                "message": r.note,
                "timestamp": ts,
                "major": r.major,
                "sub": r.sub,
                "parent_major": r.parent_major,
                "parent_sub": r.parent_sub,
                "note": r.note,
                "ts": ts,
            })
        return result

    async def max_subversion(self, wf_id: str, major: int) -> int:
        """Largest sub for ``major`` (-1 if the major has no rows)."""
        return (await self._s.execute(
            select(func.coalesce(func.max(WorkflowVersion.sub), -1))
            .where(WorkflowVersion.wf_id == wf_id,
                   WorkflowVersion.major == major)
        )).scalar_one()

    async def list_subversions(self, wf_id: str, major: int) -> list[int]:
        """All sub numbers for ``major``, ascending."""
        rows = (await self._s.execute(
            select(WorkflowVersion.sub)
            .where(WorkflowVersion.wf_id == wf_id,
                   WorkflowVersion.major == major)
            .order_by(WorkflowVersion.sub)
        )).all()
        return [r[0] for r in rows]

    async def set_head(self, wf_id: str, major: int, sub: int) -> dict:
        """Move HEAD to an explicit ``(major, sub)``. Legacy return shape:
        updated meta dict, or ``{}`` if the workflow does not exist.

        Row-lock with ``FOR UPDATE`` so the head pointer
        move (undo / redo / checkout) is serialized at the DB and
        covers the dependency-teardown commit (the asyncio lock did
        not). Missing / soft-deleted still returns ``{}`` unchanged.
        """
        w = await self._s.get(Workflow, wf_id, with_for_update=True)
        if not w or w.deleted_at is not None:
            return {}
        await self._s.execute(
            update(Workflow).where(Workflow.wf_id == wf_id)
            .values(active_major=major, active_sub=sub)
        )
        await self._s.flush()
        w = await self._s.get(Workflow, wf_id)
        return await self._meta_to_dict(w) if w else {}

    async def mark_saved(self, wf_id: str) -> None:
        """No-op under Postgres (WAL durability; no save flag/cache)."""
        pass

    # ===================================================================
    # Utility
    # ===================================================================

    def flush(self) -> None:
        """No-op under Postgres (WAL durability; caller owns commit)."""
        pass
