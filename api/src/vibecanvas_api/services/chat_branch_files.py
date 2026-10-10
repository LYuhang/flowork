"""Check current file availability after authorizing a branch's source history."""
import asyncio

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.schemas.preview import FileRefV1
from vibecanvas_api.services.object_store import get_object_store

_file_ref = TypeAdapter(FileRefV1)


async def unavailable_branch_files(*, repo, source, turn_id, request, auth, session, service):
    # Reuse Preview's ownership, path visibility, and storage resolution. A
    # saved reference alone must never authorize opening another user's file.
    from vibecanvas_api.routes.previews import _authorize_file_ref, _resolve_file

    unavailable, seen = [], set()
    offset = 0
    while True:
        rows, total, _ = await repo.list_message_page(
            source.chat_id, through_turn_id=turn_id, limit=200, offset=offset,
        )
        for row in rows:
            for attachment in row.get('content', {}).get('attachments') or []:
                resource = attachment.get('resource') or attachment.get('source')
                if isinstance(resource, dict) and resource.get('kind') == 'file':
                    ref = resource['file_ref']
                elif attachment.get('type') in {'file', 'image', 'video'} and attachment.get('path'):
                    path = attachment['path']
                    ref = {'schemaVersion': 1, 'scope': 'mount' if path.startswith('/mount/') else 'project', 'path': path}
                    if ref['scope'] == 'project':
                        ref['projectId'] = source.project_id
                else:
                    continue
                label = attachment.get('label') or attachment.get('name') or ref.get('path', 'File')
                try:
                    parsed = _file_ref.validate_python(ref)
                    key = parsed.model_dump_json()
                    if key in seen:
                        continue
                    seen.add(key)
                    await _authorize_file_ref(file_ref=parsed, request=request, auth=auth,
                        service=service, action=Action.VIEW,
                        consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
                    resolved = await _resolve_file(file_ref=parsed, auth=auth, session=session)
                    if resolved.workspace_identity is None:
                        await asyncio.to_thread(get_object_store().revision, resolved.row.object_key)
                except (ValidationError, FileNotFoundError, KeyError):
                    unavailable.append(label)
                except HTTPException as exc:
                    if exc.status_code not in {403, 404}:
                        raise
                    unavailable.append(label)
        offset += len(rows)
        if not rows or offset >= total:
            return unavailable
