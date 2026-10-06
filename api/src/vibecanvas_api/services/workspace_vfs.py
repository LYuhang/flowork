"""POSIX adapters for already-authorized VFS resource scopes."""
import os
import io
from dataclasses import dataclass
from vibecanvas_api.services.chat_workspace import is_agent_workspace_scope
from vibecanvas_api.services.file_format import content_type_for
from vibecanvas_api.services.user_mount_workspace import mount_scope_id
from vibecanvas_api.services.workspace_file_access import workspace_path_visible
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity
from vibecanvas_api.storage.vfs_store import VfsEntryMeta


@dataclass(frozen=True)
class WorkspaceFileMetadata:
    size_bytes: int
    content_type: str
    content_revision: str


def workspace_file_metadata(storage: PosixWorkspaceStorage, identity: WorkspaceIdentity,
                            relative: str) -> WorkspaceFileMetadata:
    with storage.open_read(identity, relative) as source:
        info = os.fstat(source.fileno())
    return workspace_metadata_from_stat(relative, info)


def workspace_metadata_from_stat(relative: str, info: os.stat_result) -> WorkspaceFileMetadata:
    return WorkspaceFileMetadata(
        info.st_size, content_type_for(relative),
        f'posix:{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_size}',
    )


async def signed_workspace_scope(session, *, tenant_id: str, scope_id: str) -> tuple[WorkspaceIdentity, str]:
    """Resolve a verified capability scope; this function does not grant access."""
    if scope_id.startswith('__mount_'):
        from sqlalchemy import Text, cast, func, select
        from vibecanvas_api.storage.models import User
        # Mount scope identifiers predate the storage backend. Resolve their
        # canonical user through the registry, never use the scope as a path.
        user_id = (await session.execute(select(User.user_id).where(
            func.substr(func.replace(cast(User.user_id, Text), '-', ''), 1, 24)
            == scope_id.removeprefix('__mount_')
        ))).scalar_one_or_none()
        if user_id is None:
            raise FileNotFoundError('Workspace owner not found')
        return workspace_scope(tenant_id, scope_id, str(user_id))
    return workspace_scope(tenant_id, scope_id, '')


def workspace_scope(tenant_id: str, scope_id: str, user_id: str) -> tuple[WorkspaceIdentity, str]:
    # Scope authorization must precede this adapter. Never infer a mount owner's
    # identity from the truncated scope hash supplied by a client.
    if scope_id == mount_scope_id(user_id):
        identity = WorkspaceIdentity(tenant_id, 'user_mount', user_id)
        path_prefix = '/mount/'
    elif is_agent_workspace_scope(scope_id):
        identity = WorkspaceIdentity(tenant_id, 'project', scope_id)
        path_prefix = '/'
    elif scope_id and not scope_id.startswith('__'):
        identity = WorkspaceIdentity(tenant_id, 'workflow', scope_id)
        path_prefix = '/'
    else:
        raise ValueError('Unsupported workspace scope')
    return identity, path_prefix


def read_workspace_file(storage: PosixWorkspaceStorage, *, tenant_id: str,
                        scope_id: str, user_id: str, path: str,
                        max_bytes: int) -> tuple[bytes, int]:
    """Read a bounded prefix from the same descriptor used to obtain its size."""
    identity, path_prefix = workspace_scope(tenant_id, scope_id, user_id)
    if not path.startswith(path_prefix) or max_bytes < 0:
        raise ValueError('Invalid workspace read')
    with storage.open_read(identity, path[len(path_prefix):]) as source:
        size = os.fstat(source.fileno()).st_size
        return source.read(max_bytes), size


def write_workspace_file(storage: PosixWorkspaceStorage, *, tenant_id: str,
                         scope_id: str, user_id: str, path: str, data: bytes) -> bool:
    identity, path_prefix = workspace_scope(tenant_id, scope_id, user_id)
    if not path.startswith(path_prefix):
        raise ValueError('Invalid workspace write path')
    return storage.write_file(identity, path[len(path_prefix):], io.BytesIO(data))


def remove_workspace_path(storage: PosixWorkspaceStorage, *, tenant_id: str,
                          scope_id: str, user_id: str, path: str) -> int:
    identity, prefix = workspace_scope(tenant_id, scope_id, user_id)
    if not path.startswith(prefix):
        raise ValueError('Invalid workspace path')
    return storage.remove_path(identity, path[len(prefix):])


def rename_workspace_path(storage: PosixWorkspaceStorage, *, tenant_id: str,
                          scope_id: str, user_id: str, source: str, destination: str) -> None:
    identity, prefix = workspace_scope(tenant_id, scope_id, user_id)
    if not source.startswith(prefix) or not destination.startswith(prefix):
        raise ValueError('Invalid workspace path')
    storage.rename_path(identity, source[len(prefix):], destination[len(prefix):])


def list_workspace_files(storage: PosixWorkspaceStorage, *, tenant_id: str,
                         scope_id: str, user_id: str, prefix: str,
                         owned_chats: set[str]) -> list[VfsEntryMeta]:
    identity, path_prefix = workspace_scope(tenant_id, scope_id, user_id)

    def visible(relative: str) -> bool:
        return workspace_path_visible(path_prefix + relative, owned_chats)

    out = []
    for entry in storage.entries(identity, visible=visible):
        path = path_prefix + entry.path
        if entry.is_directory:
            # The existing Explorer uses virtual keep entries for empty folders.
            path += '/.vibekeep'
        if not path.startswith(prefix):
            continue
        out.append(VfsEntryMeta(
            path=path, kind='artifact', content_type=content_type_for(path),
            abstract='', size_bytes=entry.size_bytes, wf_version=None,
            last_access=entry.modified_ns / 1_000_000_000,
        ))
    return out
