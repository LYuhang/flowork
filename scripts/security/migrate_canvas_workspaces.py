#!/usr/bin/env python3
"""Relocate canvas private files and runtimes while API/sandbox writers are stopped.

Defaults to a read-only inventory. Flush resident sandbox snapshots before
stopping services; apply before starting the new version.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from vibecanvas_api.security.canvas_workspace_migration import migrate_canvas_project_files, migrate_canvas_runtime
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.storage.db import session_scope


async def run(*, apply: bool) -> dict:
    async with session_scope_admin() as session:
        projects = (await session.execute(text(
            'SELECT tenant_id::text, creator_user_id::text, project_id, workflow_id, deleted_at IS NULL '
            'FROM chat_projects WHERE workflow_id IS NOT NULL ORDER BY tenant_id, project_id'
        ))).all()
    files = runtime_files = 0
    store = get_object_store()
    for tenant, user, project, workflow, active in projects:
        async with session_scope(tenant_id=tenant, user_id=user) as session:
            files += await migrate_canvas_project_files(session, store,
                project_id=project, apply=apply)
            if active:
                runtime_files += await asyncio.to_thread(migrate_canvas_runtime, store,
                    tenant_id=tenant, user_id=user, workflow_id=workflow, project_id=project, apply=apply)
    return {'apply': apply, 'projects_checked': len(projects), 'files': files, 'runtime_files': runtime_files}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Move file rows; requires stopped writers.')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(apply=args.apply))))
