"""One object-backed /run per Deployment, shared across resident revisions.

The daemon owns the plaintext projection. Invocation completion and session
shutdown flush it; neither action clears it. History remains invocation-scoped.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile
import uuid

from sqlalchemy import delete

from vibecanvas_api.services.file_format import content_type_for
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.workflow_artifacts import _files, restore_workflow_artifacts
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.models import VfsRun
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo


class DeploymentWorkspace:
    def __init__(self, tenant_id: str, deployment_id: str):
        self.tenant_id = str(uuid.UUID(tenant_id))
        self.run_id = f"deployment-run-{uuid.UUID(deployment_id).hex}"
        self.directory = tempfile.TemporaryDirectory(prefix="fw-deploy-run-")
        self.root = Path(self.directory.name)
        self.lock = asyncio.Lock()
        self.owners: set[str] = set()
        self.digests: dict[str, str] = {}

    async def hydrate(self):
        await restore_workflow_artifacts(root=self.root, tenant_id=self.tenant_id, execution_id=self.run_id)
        self.digests = await asyncio.to_thread(self._digests)

    def _digests(self):
        return {relative: hashlib.sha256(data).hexdigest() for relative, data in _files(str(self.root))}

    async def sync(self):
        async with self.lock:
            # Serialize syncs, not executions. Workflow authors own concurrent
            # writes to the same file; unchanged files incur no object writes.
            current: dict[str, str] = {}
            iterator = _files(str(self.root))
            sentinel = object()
            try:
                while True:
                    item = await asyncio.to_thread(next, iterator, sentinel)
                    if item is sentinel:
                        break
                    relative, data = item
                    digest = hashlib.sha256(data).hexdigest()
                    current[relative] = digest
                    if self.digests.get(relative) == digest:
                        continue
                    async with short_session_scope(tenant_id=self.tenant_id) as db:
                        await VfsRunRepo(db, get_object_store(), self.tenant_id).write_bytes(
                            run_id=self.run_id, path="/run/" + relative, data=data,
                            content_type=content_type_for(relative, data),
                            # Do not let canvas run cleanup purge this namespace.
                            wf_id=None,
                        )
                    self.digests[relative] = digest
            finally:
                iterator.close()
            for relative in self.digests.keys() - current.keys():
                async with short_session_scope(tenant_id=self.tenant_id) as db:
                    await db.execute(delete(VfsRun).where(
                        VfsRun.run_id == self.run_id, VfsRun.path == "/run/" + relative))
                await asyncio.to_thread(get_object_store().delete_bytes,
                                        f"run/{self.tenant_id}/{self.run_id}/{relative}")
            self.digests = current


class DeploymentWorkspaces:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.entries: dict[tuple[str, str], DeploymentWorkspace] = {}

    async def acquire(self, tenant_id: str, deployment_id: str, owner: str):
        key = (str(uuid.UUID(tenant_id)), str(uuid.UUID(deployment_id)))
        async with self.lock:
            workspace = self.entries.get(key)
            if workspace is None:
                workspace = DeploymentWorkspace(*key)
                try:
                    await workspace.hydrate()
                except BaseException:
                    workspace.directory.cleanup()
                    raise
                self.entries[key] = workspace
            workspace.owners.add(owner)
            return workspace

    async def release(self, tenant_id: str, deployment_id: str, owner: str):
        key = (str(uuid.UUID(tenant_id)), str(uuid.UUID(deployment_id)))
        async with self.lock:
            workspace = self.entries.get(key)
            if workspace is None:
                return
            await workspace.sync()  # Failure retains source and ownership for retry.
            workspace.owners.discard(owner)
            if not workspace.owners:
                workspace.directory.cleanup()
                del self.entries[key]
