"""One-time, offline relocation of canvas private files into Project scopes.

Run after stopping API/sandbox writers. This is deployment tooling, not a
fallback on the request path. Database rows move transactionally; old encrypted
objects remain unreferenced until normal storage cleanup.
"""
from sqlalchemy import select

from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.storage.models import Chat, ChatProject, VfsArtifact, VfsScratch, Workflow
from vibecanvas_api.storage.vfs_store import VfsRepo


async def migrate_canvas_project_files(session, store, *, project_id: str, apply: bool = False) -> int:
    project = await session.get(ChatProject, project_id)
    if project is None or not project.workflow_id:
        raise ValueError('canvas_project_required')
    workflow = await session.get(Workflow, project.workflow_id)
    chats = set((await session.execute(select(Chat.chat_id).where(
        Chat.project_id == project_id, Chat.creator_user_id == project.creator_user_id,
    ))).scalars())
    scope = project_workspace_scope_id(project_id)
    repo = VfsRepo(session, object_store=store)
    moved = 0
    for model in (VfsArtifact, VfsScratch):
        rows = (await session.execute(select(model).where(model.scope_id == project.workflow_id))).scalars().all()
        for row in rows:
            parts = row.path.strip('/').split('/')
            if parts[0] == 'chats':
                owned = len(parts) >= 3 and parts[1] in chats
            else:
                # Before resource sharing, these workspace roots belonged to
                # the workflow creator. Never guess ownership for a recipient.
                owned = (parts[0] in {'data', 'memory', 'logs'} and project.deleted_at is None and workflow is not None
                         and workflow.creator_user_id == project.creator_user_id)
            if not owned:
                continue
            source = await repo.read(wf_id=project.workflow_id, path=row.path, touch=False)
            data = await repo.read_bytes(wf_id=project.workflow_id, path=row.path)
            if source is None or data is None:
                raise ValueError('canvas_migration_source_missing')
            existing = await repo.read(wf_id=scope, path=row.path, touch=False)
            if existing is not None and (existing.content_type != source.content_type
                    or await repo.read_bytes(wf_id=scope, path=row.path) != data):
                raise ValueError('canvas_migration_destination_conflict')
            if apply:
                if existing is None:
                    writer = repo.write_scratch_bytes if model is VfsScratch else repo.upsert_internal_artifact_bytes
                    await writer(wf_id=scope, tenant=str(project.tenant_id), path=row.path,
                                 data=data, content_type=source.content_type, abstract=source.abstract)
                await session.delete(row)
            moved += 1
    await session.flush()
    return moved


def migrate_canvas_runtime(store, *, tenant_id: str, user_id: str, workflow_id: str,
                           project_id: str, apply: bool = False) -> int:
    """Move an encrypted runtime snapshot between scopes of the SAME user.

    Callers select the owning Project from the database. All writers must be
    stopped and their runtime snapshots flushed before applying this move.
    Existing destinations are checked before any write. A partially completed
    copy can be retried; source deletion starts only after verification.
    """
    from pathlib import PurePosixPath
    from vibecanvas_api.services.vfs_volume import EncryptedObjectStoreProjectRuntimeVolumeProvider
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    identity = dict(tenant_id=tenant_id, user_id=user_id)
    source = provider._coordinates(**identity, project_scope_id=workflow_id)[3] + '/'
    target = provider._coordinates(**identity, project_scope_id=project_workspace_scope_id(project_id))[3] + '/'
    if source == target:
        raise ValueError('canvas_runtime_source_equals_destination')
    keys = list(store.list_keys(source))
    transfers = []
    target_keys = set(store.list_keys(target))
    for key in keys:
        relative = key.removeprefix(source)
        path = PurePosixPath(relative)
        if not key.startswith(source) or path.is_absolute() or '..' in path.parts or '\x00' in relative:
            raise ValueError('invalid_canvas_runtime_path')
        if relative == '.codex/auth.json':
            continue  # Authority remains the user account cache, not a snapshot.
        destination = target + relative
        data = store.fetch_bytes(key)
        if destination in target_keys and store.fetch_bytes(destination) != data:
            raise ValueError('canvas_runtime_destination_conflict')
        transfers.append((key, destination))
    if apply:
        for key, destination in transfers:
            if destination not in target_keys:
                store.put_bytes(destination, store.fetch_bytes(key))
        for key, destination in transfers:
            if store.fetch_bytes(destination) != store.fetch_bytes(key):
                raise ValueError('canvas_runtime_verification_failed')
        for key in keys:
            store.delete_bytes(key)
    return len(transfers)
