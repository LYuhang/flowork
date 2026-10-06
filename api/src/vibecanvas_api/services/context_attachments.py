"""Authorize and resolve user context before it crosses the Runtime boundary.

Neither a resource ID nor a user-supplied snapshot grants read access. Resolved
text is user input and never an instruction or a tool result.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from vibecanvas_api.authorization.dependencies import principal_for_auth, context_for_auth
from vibecanvas_api.authorization.types import Action, ConsistencyPreference, ResourceRef, ResourceType
from vibecanvas_api.schemas.context_attachments import (
    FileResource, MessageSource, QuoteContextAttachment,
    WorkflowSelection,
)
from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.file_revision import vfs_row_revision
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.models import ChatMessage
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

MAX_CONTEXT_ATTACHMENTS = 32
MAX_CONTEXT_TEXT = 65536


from vibecanvas_api.services.agent_runtime.context_attachments import context_text


def workflow_selection_snapshot(workflow: dict, selector: WorkflowSelection | None) -> dict:
    nodes = {key: value for key, value in workflow.items()
             if not key.startswith('__') and isinstance(value, dict)}
    if selector is None:
        # A whole workflow reference is bounded inventory; its full version is
        # available through the workflow CLI, not silently cut off in a prompt.
        return {'nodes': [{'id': key, 'name': value.get('name'), 'type': value.get('node_type')}
                          for key, value in nodes.items()]}
    selected = set(selector.node_ids)
    for edge in selector.edges:
        source = nodes.get(edge.source)
        if source is None or edge.target not in (source.get('children') or []):
            raise HTTPException(422, 'context_workflow_edge_not_found')
        # The current workflow stores business edges as source/target pairs.
        # Canvas-only handles must never claim a separate persisted identity.
        if edge.source_handle is not None or edge.target_handle is not None:
            raise HTTPException(422, 'context_workflow_edge_handle_not_supported')
        selected.update((edge.source, edge.target))
    if not selected.issubset(nodes):
        raise HTTPException(422, 'context_workflow_node_not_found')
    return {'nodes': {key: nodes[key] for key in sorted(selected)},
            'selected_edges': [edge.model_dump(exclude_none=True) for edge in selector.edges]}


class ContextResolver:
    def __init__(self, *, session, auth, request, service, chat_id: str):
        self.session, self.auth, self.request, self.service = session, auth, request, service
        self.chat_id = chat_id
        self.chat_repo = ChatRepo(session, auth.user_id)
        self.cache: dict[str, Any] = {}
        self.durable: list[dict] = []

    async def authorize(self, resource_type, resource_id, action=Action.VIEW):
        decision = await self.service.check(
            principal_for_auth(self.auth), action,
            ResourceRef(resource_type, resource_id, self.auth.active_organization_id),
            context_for_auth(self.auth, self.request,
                consistency=ConsistencyPreference.HIGHER_CONSISTENCY),
        )
        if not decision.allowed:
            raise HTTPException(404, 'context_resource_not_found')

    async def source(self, resource):
        key = resource.model_dump_json()
        if key in self.cache:
            return self.cache[key]
        if isinstance(resource, MessageSource):
            await self.authorize(ResourceType.CHAT, resource.chat_id)
            row = (await self.session.execute(select(ChatMessage.message_id).where(
                ChatMessage.chat_id == resource.chat_id,
                ChatMessage.message_id == resource.message_id,
            ))).scalar_one_or_none()
            source_chat = await self.chat_repo.get_authorized_inventory(resource.chat_id)
            if row is None or source_chat is None or str(source_chat['creator_user_id']) != str(self.auth.user_id):
                raise HTTPException(404, 'context_message_not_found')
            result = {}
        elif isinstance(resource, FileResource):
            # Shared Preview policy authorizes the actual Project/run/storage
            # object. It additionally rejects another user's Project identity.
            from vibecanvas_api.routes.previews import _authorize_file_ref, _resolve_file
            await _authorize_file_ref(file_ref=resource.file_ref, request=self.request,
                auth=self.auth, service=self.service, action=Action.VIEW,
                consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
            result = await _resolve_file(file_ref=resource.file_ref, auth=self.auth, session=self.session)
        elif resource.kind == 'workflow':
            await self.authorize(ResourceType.WORKFLOW, resource.workflow_id)
            repo = WorkflowRepo(self.session, self.auth.user_id)
            if not await repo.get_meta(resource.workflow_id):
                raise HTTPException(404, 'context_workflow_not_found')
            major, sub = resource.version[1:].split('.sv')
            result = await repo.get_workflow_at(resource.workflow_id, int(major), int(sub))
            if not result:
                raise HTTPException(404, 'context_workflow_version_not_found')
        elif resource.kind == 'artifact':
            from vibecanvas_api.storage.hitl_repo import HitlRepo
            await self.authorize(ResourceType.CHAT, resource.chat_id)
            await self.authorize(ResourceType.INTERACTIVE_ARTIFACT, resource.artifact_id)
            result = await HitlRepo(self.session).get_artifact_for_user(resource.artifact_id, self.auth.user_id)
            if result is None or result.chat_id != resource.chat_id:
                raise HTTPException(404, 'context_artifact_not_found')
        elif resource.kind == 'job':
            from vibecanvas_api.storage.background_jobs_repo import BackgroundJobsRepo
            await self.authorize(ResourceType.CHAT, resource.chat_id)
            await self.authorize(ResourceType.BACKGROUND_JOB, resource.job_id)
            result = await BackgroundJobsRepo(self.session).get_for_user(
                chat_id=resource.chat_id, job_id=resource.job_id, creator_user_id=self.auth.user_id)
            if result is None:
                raise HTTPException(404, 'context_job_not_found')
            if resource.execution_id is not None:
                # parent_run_id is an Agent Turn, never a Workflow execution.
                # Bind only an execution actually returned by this job.
                result_payload = result.result_snapshot or {}
                execution_id = result_payload.get('exec_id') or result_payload.get('execution_id')
                if execution_id != resource.execution_id:
                    raise HTTPException(422, 'context_job_execution_mismatch')
                await self.authorize(ResourceType.WORKFLOW_EXECUTION, execution_id)
        else:
            # URL references do not perform a host fetch (and confer no SSRF
            # capability). Reading the page remains an explicit Agent action.
            result = {'url': resource.url}
        self.cache[key] = result
        return result

    async def resolve(self, attachments: list, *, materialize: bool = False) -> list[dict]:
        if len(attachments) > MAX_CONTEXT_ATTACHMENTS:
            raise HTTPException(422, 'too_many_context_attachments')
        if not attachments:
            return []
        inventory = await self.chat_repo.get_authorized_inventory(self.chat_id)
        if inventory is None:
            raise HTTPException(404, 'chat_not_found')
        output = []
        self.durable = []
        total = 0
        for item in attachments:
            payload = item.model_dump(mode='json', exclude_none=True, by_alias=True)
            if getattr(item, 'schema_version', None) != 1:
                if item.type in {'file', 'image', 'video'}:
                    from vibecanvas_api.schemas.preview import ProjectFileRefV1, MountFileRefV1
                    file_ref = (MountFileRefV1(schemaVersion=1, scope='mount', path=item.path)
                        if item.path.startswith('/mount/') else ProjectFileRefV1(
                            schemaVersion=1, scope='project', projectId=inventory['project_id'], path=item.path))
                    await self.source(FileResource(kind='file', file_ref=file_ref))
                output.append(payload)
                self.durable.append(dict(payload))
                continue
            resource = item.source if isinstance(item, QuoteContextAttachment) else item.resource
            resolved = await self.source(resource)
            if isinstance(item, QuoteContextAttachment):
                # The snapshot represents user-selected text (including rendered
                # Markdown or unsaved edits), not a claim of byte-identical source.
                total += len(context_text(payload))
            else:
                summary: dict = {'resource': resource.model_dump(mode='json', by_alias=True), 'label': item.label}
                if item.type == 'resource' and item.selector is not None:
                    summary['selector'] = item.selector.model_dump(mode='json', by_alias=True)
                if resource.kind == 'workflow':
                    summary['snapshot'] = workflow_selection_snapshot(resolved, item.selector)
                elif resource.kind == 'file':
                    revision = vfs_row_revision(resolved.row)
                    if resource.revision is not None and resource.revision != revision:
                        raise HTTPException(409, 'context_file_revision_changed')
                    summary['revision'] = revision
                    summary['path'] = resource.file_ref.path
                    if materialize:
                        path = await self.materialize_file(resolved, inventory)
                        payload.update(path=path, name=posixpath.basename(path),
                            content_type=resolved.row.content_type or 'application/octet-stream')
                        summary['runtime_path'] = path
                elif resource.kind == 'artifact':
                    summary['component_type'] = resolved.component_type
                    summary['definition'] = resolved.definition_json
                elif resource.kind == 'job':
                    summary['snapshot'] = {'status': resolved.status, 'title': resolved.title,
                        'result': resolved.result_snapshot, 'error': resolved.error_json}
                payload['resolved_text'] = json.dumps(summary, ensure_ascii=False, default=str)
                total += len(payload['resolved_text'])
            if total > MAX_CONTEXT_TEXT:
                raise HTTPException(413, 'context_too_large_select_a_smaller_excerpt')
            durable = item.model_dump(mode='json', exclude_none=True, by_alias=True)
            if item.type in {'file', 'resource'}:
                durable['snapshot'] = {'text': payload['resolved_text']}
            self.durable.append(durable)
            output.append(payload)
        return output

    async def materialize_file(self, resolved, inventory: dict) -> str:
        # The Project workspace is already persistent and available to this
        # chat's sandbox. A reference does not create a second copy of it.
        if (resolved.file_ref.scope == 'project'
                and resolved.file_ref.project_id == inventory['project_id']):
            return resolved.file_ref.path
        from vibecanvas_api.services.object_store import get_object_store
        from vibecanvas_api.storage.vfs_store import VfsRepo
        from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
        from vibecanvas_api.config import config
        if resolved.row.size_bytes > config.storage.vfs_upload_max_bytes:
            raise HTTPException(413, 'context_file_too_large')
        from vibecanvas_api.routes.previews import _source_prefix
        data = await asyncio.to_thread(_source_prefix, resolved, config.storage.vfs_upload_max_bytes + 1)
        if len(data) > config.storage.vfs_upload_max_bytes:
            raise HTTPException(413, 'context_file_too_large')
        # Copy into the destination Project so run/mount/other Project files are
        # actually readable. Content identity prevents overwriting old snapshots.
        digest = hashlib.sha256(data).hexdigest()
        name = posixpath.basename(resolved.file_ref.path)
        path = f'/chats/{self.chat_id}/contexts/{digest[:24]}/{name}'
        scope = project_workspace_scope_id(inventory['project_id'])
        if config.workspace_storage_backend == 'posix':
            from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
            from vibecanvas_api.services.workspace_vfs import write_workspace_file
            await asyncio.to_thread(
                write_workspace_file, PosixWorkspaceStorage(config.workspace_storage_root),
                tenant_id=self.auth.tenant_id, scope_id=scope, user_id=self.auth.user_id,
                path=path, data=data,
            )
            return path
        store = get_object_store()
        await VfsRepo(self.session, object_store=store).upsert_artifact_bytes(
            wf_id=scope, tenant=self.auth.tenant_id, path=path, data=data,
            content_type=resolved.row.content_type or 'application/octet-stream')
        # A failed projection fails dispatch, allowing retry. Do not start an
        # Agent claiming a file exists when a warm sandbox cannot yet read it.
        await get_sandbox_manager().mirror_vfs_write(self.auth.tenant_id, scope, path, data)
        return path
