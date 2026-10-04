import pytest
from sqlalchemy import update

from tests.test_chat_repo_pg import _seed_and_bind, USER, PROJECT
from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.workspace_file_access import owned_workspace_chats, workspace_path_visible
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.models import Chat, ChatProject


@pytest.mark.parametrize('path,allowed', [
    ('/chats/own/notes.txt', True), ('/chats/other/notes.txt', False),
    ('/chats', True), ('/./chats/other/notes.txt', False),
    ('/chats/own/../other/notes.txt', False), ('/run/result.csv', True),
])
def test_private_chat_paths(path, allowed):
    assert workspace_path_visible(path, {'own'}) is allowed


@pytest.mark.asyncio
async def test_private_chat_membership_checks_owner_scope_and_project_lifetime(pg_session):
    await _seed_and_bind(pg_session)
    chat_id = await ChatRepo(pg_session, str(USER)).register_session('test-private-paths', project_id=PROJECT)
    scope = project_workspace_scope_id(PROJECT)
    assert chat_id in await owned_workspace_chats(pg_session, scope, str(USER))
    assert not await owned_workspace_chats(pg_session, scope, '00000000-0000-0000-0000-000000000000')
    assert not await owned_workspace_chats(pg_session, 'another-workflow', str(USER))
    from sqlalchemy import func
    await pg_session.execute(update(Chat).where(Chat.chat_id == chat_id).values(deleted_at=func.now()))
    assert chat_id in await owned_workspace_chats(pg_session, scope, str(USER))
    await pg_session.execute(update(ChatProject).where(ChatProject.project_id == PROJECT).values(deleted_at=func.now()))
    assert not await owned_workspace_chats(pg_session, scope, str(USER))
