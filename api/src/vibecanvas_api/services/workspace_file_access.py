"""Private Chat paths remain private inside a shared Workflow workspace."""
import posixpath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.services.chat_workspace import project_id_from_workspace_scope
from vibecanvas_api.storage.models import Chat, ChatProject


async def owned_workspace_chats(session: AsyncSession, scope_id: str, user_id: str) -> set[str]:
    # Deleting a conversation retains its files in the private Project.
    # Ownership remains authoritative until the Project itself is deleted.
    project_id = project_id_from_workspace_scope(scope_id)
    scope = ChatProject.project_id == project_id if project_id else ChatProject.workflow_id == scope_id
    return set((await session.execute(
        select(Chat.chat_id).join(ChatProject, ChatProject.project_id == Chat.project_id).where(
            scope, Chat.creator_user_id == user_id, ChatProject.creator_user_id == user_id,
            ChatProject.deleted_at.is_(None),
        )
    )).scalars())


def workspace_path_visible(path: str, owned_chats: set[str]) -> bool:
    parts = posixpath.normpath(path).strip('/').split('/')
    if parts[0] != 'chats':
        return True
    return bool(owned_chats) if len(parts) == 1 else parts[1] in owned_chats
