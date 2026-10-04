"""Persistence for Project-owned Agent workspaces."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import content_encryption_service
from vibecanvas_api.storage.models import Chat, ChatProject, ProjectMcpBinding
from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.storage.vfs_store import VfsRepo
from vibecanvas_api.services.object_store import get_object_store


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ChatProjectRepo:
    def __init__(self, session: AsyncSession, user_id: str) -> None:
        self._session = session
        self._user_id = user_id

    async def commit(self) -> None:
        await self._session.commit()

    async def _tenant_id(self) -> uuid.UUID:
        value = (
            await self._session.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            )
        ).scalar_one()
        if not value:
            raise RuntimeError("tenant context is required for Project metadata")
        return uuid.UUID(str(value))

    async def _store_name(self, project: ChatProject, name: str) -> None:
        encrypted = await content_encryption_service().encrypt_json(
            self._session,
            tenant_id=project.tenant_id,
            resource_type="organization_metadata",
            resource_id=str(project.tenant_id),
            purpose="chat_project_metadata",
            record_id=project.project_id,
            value={"name": name, "meta": {}},
        )
        project.metadata_ciphertext = encrypted.ciphertext
        project.metadata_nonce = encrypted.nonce
        project.metadata_key_id = encrypted.key_id
        project.name = name

    async def _materialize(self, project: ChatProject) -> ChatProject:
        value = await content_encryption_service().decrypt_json(
            self._session,
            key_id=project.metadata_key_id,
            tenant_id=project.tenant_id,
            resource_type="organization_metadata",
            resource_id=str(project.tenant_id),
            purpose="chat_project_metadata",
            record_id=project.project_id,
            ciphertext=project.metadata_ciphertext,
            nonce=project.metadata_nonce,
        )
        project.name = (
            str(value.get("name") or "Untitled project")
            if isinstance(value, dict)
            else "Untitled project"
        )
        return project

    async def create(self, *, name: str, project_id: str | None = None, surface: str = "chat", workflow_id: str | None = None) -> dict:
        from .agent_runtime_repo import AgentRuntimeRepo

        preferences = await AgentRuntimeRepo(self._session, self._user_id).get_preferences()
        runtime_type = preferences["default_runtime_type"]
        if surface not in {"chat", "browser"}:
            raise ValueError("unsupported Project surface")
        project = ChatProject(
            project_id=project_id or f"prj_{uuid.uuid4().hex}",
            tenant_id=await self._tenant_id(),
            creator_user_id=self._user_id,
            surface=surface,
            workflow_id=workflow_id,
            runtime_type=runtime_type,
            runtime_session_id=f"rt_{runtime_type}_{uuid.uuid4().hex}",
        )
        await self._store_name(project, name)
        self._session.add(project)
        await self._session.flush()
        files = VfsRepo(self._session, object_store=get_object_store())
        scope_id = project_workspace_scope_id(project.project_id)
        for folder in ("data", "logs", "chats"):
            await files.upsert_internal_artifact_bytes(
                wf_id=scope_id, tenant=str(project.tenant_id),
                path=f"/{folder}/.keep", data=b"", content_type="application/x-directory",
            )
        await files.write_scratch(
            wf_id=scope_id, tenant=str(project.tenant_id),
            path="/memory/.keep", content="", content_type="application/x-directory",
        )
        return self._project(project)

    async def for_workflow(self, workflow_id: str) -> ChatProject:
        """Get/create the owner's hidden Project under a transaction fence.

        Workflow authorization is performed by the caller. A previously deleted
        Project remains deleted; a later new conversation receives a new Project.
        """
        tenant_id = await self._tenant_id()
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"workflow-chat-project:{tenant_id}:{self._user_id}:{workflow_id}"},
        )
        project = await self.find_for_workflow(workflow_id)
        if project is None:
            created = await self.create(name="Workflow chats", workflow_id=workflow_id)
            project = await self.get(created["project_id"])
        if project is None:
            raise RuntimeError("Workflow Chat Project was not persisted")
        return project

    async def find_for_workflow(self, workflow_id: str) -> ChatProject | None:
        """Resolve an existing private workspace without creating resources."""
        return (await self._session.execute(select(ChatProject).where(
            ChatProject.workflow_id == workflow_id,
            ChatProject.creator_user_id == self._user_id,
            ChatProject.deleted_at.is_(None),
        ))).scalar_one_or_none()

    async def get(self, project_id: str, *, for_update: bool = False) -> ChatProject | None:
        query = select(ChatProject).where(
            ChatProject.project_id == project_id,
            ChatProject.creator_user_id == self._user_id,
            ChatProject.deleted_at.is_(None),
        )
        if for_update:
            query = query.with_for_update()
        project = (await self._session.execute(query)).scalar_one_or_none()
        return await self._materialize(project) if project is not None else None

    async def get_mcp_selection(self, project_id: str) -> dict | None:
        revision = (await self._session.execute(
            select(ChatProject.mcp_config_revision).where(
                ChatProject.project_id == project_id,
                ChatProject.creator_user_id == self._user_id,
                ChatProject.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if revision is None:
            return None
        ids = (await self._session.execute(
            select(ProjectMcpBinding.mcp_server_id)
            .where(ProjectMcpBinding.project_id == project_id)
            .order_by(ProjectMcpBinding.mcp_server_id)
        )).scalars().all()
        return {
            "mcp_server_ids": [str(item) for item in ids],
            "mcp_config_revision": int(revision),
        }

    async def set_mcp_selection(
        self,
        project_id: str,
        *,
        mcp_server_ids: list[uuid.UUID],
        expected_revision: int,
    ) -> dict:
        """CAS-update one Project's complete selected custom-MCP set."""
        project = (await self._session.execute(
            select(ChatProject).where(
                ChatProject.project_id == project_id,
                ChatProject.creator_user_id == self._user_id,
                ChatProject.deleted_at.is_(None),
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if project is None:
            return {"ok": False, "error_code": "project_not_found"}
        current_ids = set((await self._session.execute(
            select(ProjectMcpBinding.mcp_server_id).where(
                ProjectMcpBinding.project_id == project_id
            )
        )).scalars().all())
        desired_ids = set(mcp_server_ids)
        current_revision = int(project.mcp_config_revision or 0)
        if current_revision != expected_revision and current_ids != desired_ids:
            return {
                "ok": False,
                "error_code": "mcp_config_revision_conflict",
                "mcp_server_ids": sorted(map(str, current_ids)),
                "mcp_config_revision": current_revision,
            }
        if current_ids != desired_ids:
            await self._session.execute(
                delete(ProjectMcpBinding).where(ProjectMcpBinding.project_id == project_id)
            )
            for server_id in sorted(desired_ids, key=str):
                self._session.add(ProjectMcpBinding(
                    project_id=project_id,
                    mcp_server_id=server_id,
                    tenant_id=project.tenant_id,
                ))
            current_revision += 1
            project.mcp_config_revision = current_revision
            project.updated_at = _now()
            await self._session.flush()
        return {
            "ok": True,
            "mcp_server_ids": sorted(map(str, desired_ids)),
            "mcp_config_revision": current_revision,
        }

    async def list(self, *, surface: str = "chat") -> list[dict]:
        projects = list((await self._session.execute(
            select(ChatProject).where(
                ChatProject.creator_user_id == self._user_id,
                ChatProject.surface == surface,
                ChatProject.workflow_id.is_(None),
                ChatProject.deleted_at.is_(None),
            ).order_by(ChatProject.updated_at.desc(), ChatProject.created_at.desc())
        )).scalars().all())
        if not projects:
            return []
        project_ids = [project.project_id for project in projects]
        activity_rows = (await self._session.execute(
            select(
                Chat.project_id,
                func.count(Chat.chat_id),
                func.max(func.coalesce(Chat.last_message_at, Chat.created_at)),
            ).where(
                Chat.project_id.in_(project_ids),
                Chat.deleted_at.is_(None),
                Chat.last_message_at.is_not(None),
            ).group_by(Chat.project_id)
        )).all()
        activity = {
            str(project_id): (int(count), last_activity)
            for project_id, count, last_activity in activity_rows
        }
        values = []
        for project in projects:
            await self._materialize(project)
            count, last_activity = activity.get(project.project_id, (0, None))
            values.append(self._project(
                project,
                chat_count=count,
                last_activity_at=last_activity,
            ))
        return sorted(
            values,
            key=lambda item: item["last_activity_at"] or item["updated_at"],
            reverse=True,
        )

    async def rename(self, project_id: str, name: str) -> dict | None:
        project = await self.get(project_id, for_update=True)
        if project is None:
            return None
        await self._store_name(project, name)
        project.updated_at = _now()
        await self._session.flush()
        return self._project(project)

    async def touch(self, project_id: str) -> None:
        await self._session.execute(
            update(ChatProject).where(
                ChatProject.project_id == project_id,
                ChatProject.creator_user_id == self._user_id,
                ChatProject.deleted_at.is_(None),
            ).values(updated_at=_now())
        )

    async def soft_delete(self, project_id: str) -> bool:
        result = await self._session.execute(
            update(ChatProject).where(
                ChatProject.project_id == project_id,
                ChatProject.creator_user_id == self._user_id,
                ChatProject.deleted_at.is_(None),
            ).values(deleted_at=_now(), updated_at=_now())
        )
        return bool(result.rowcount)

    async def chat_ids(self, project_id: str) -> list[str]:
        return list((await self._session.execute(
            select(Chat.chat_id).where(
                Chat.project_id == project_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            ).order_by(Chat.created_at)
        )).scalars().all())

    @staticmethod
    def _project(
        project: ChatProject,
        *,
        chat_count: int = 0,
        last_activity_at: datetime | None = None,
    ) -> dict:
        return {
            "project_id": project.project_id,
            "workflow_id": project.workflow_id,
            "surface": project.surface,
            "name": project.name or "Untitled project",
            "runtime_type": project.runtime_type,
            "runtime_connection_id": project.runtime_connection_id,
            "runtime_model_id": project.runtime_model_id,
            "chat_count": chat_count,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "last_activity_at": (
                last_activity_at.isoformat() if last_activity_at else None
            ),
        }
