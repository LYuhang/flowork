#!/usr/bin/env python3
"""Inventory Project/Workflow files and private runtimes without reading payloads.

Run with the existing service configuration. The private JSONL output contains
storage coordinates, not file contents. Run artifacts and Task result objects
require their own execution-to-resource mapping before the final cutover.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import text

from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.services.user_mount_workspace import mount_scope_id
from vibecanvas_api.services.vfs_volume import _volume_scope
from vibecanvas_api.storage.repo_skills import skill_scope_id


async def inventory(output: Path) -> dict:
    async with session_scope_admin() as session:
        projects = (await session.execute(text(
            'SELECT tenant_id::text, creator_user_id::text, project_id, deleted_at IS NOT NULL '
            'FROM chat_projects'
        ))).all()
        workflows = (await session.execute(text(
            'SELECT tenant_id::text, wf_id, deleted_at IS NOT NULL FROM workflows'
        ))).all()
        users = (await session.execute(text('SELECT user_id::text FROM users'))).scalars().all()
        files = (await session.execute(text(
            "SELECT 'artifact', tenant_id::text, scope_id, path, object_key, size_bytes FROM vfs_artifacts "
            "UNION ALL SELECT 'scratch', tenant_id::text, scope_id, path, object_key, size_bytes FROM vfs_scratch"
        ))).all()
        runs = (await session.execute(text(
            'SELECT tenant_id::text, run_id, path, object_key, size_bytes, last_access FROM vfs_run'
        ))).all()
        executions = (await session.execute(text(
            'SELECT tenant_id::text, id::text, source_type, source_id FROM workflow_execution_runs'
        ))).all()
        tasks = (await session.execute(text('SELECT tenant_id::text, id::text FROM tasks'))).all()
        deployments = (await session.execute(text(
            'SELECT tenant_id::text, id::text, deleted_at IS NOT NULL FROM deployments'
        ))).all()
        scheduled = (await session.execute(text(
            'SELECT e.tenant_id::text, e.id::text, s.task_id::text '
            'FROM scheduled_run_executions e JOIN task_schedules s ON e.schedule_id=s.id'
        ))).all()
        current_runs = set((await session.execute(text(
            'SELECT tenant_id::text, wf_id, turn_id FROM workflow_run_state '
            'WHERE tenant_id=workflow_tenant_id'
        ))).all())
    owners = {(tenant, project_workspace_scope_id(project)): ('project', project_workspace_scope_id(project), deleted)
              for tenant, _user, project, deleted in projects}
    owners.update({(tenant, workflow): ('workflow', workflow, deleted)
                   for tenant, workflow, deleted in workflows})
    mounts = {mount_scope_id(user): user for user in users}
    skill_scopes = {skill_scope_id(user) for user in users}
    if len(mounts) != len(users):
        raise ValueError('Ambiguous user mount scope')
    counts = Counter()
    run_owners = {(tenant, workflow): ('workflow', workflow, deleted)
                  for tenant, workflow, deleted in workflows}
    resource_owners = {(tenant, 'workflow', workflow): deleted for tenant, workflow, deleted in workflows}
    resource_owners.update({(tenant, 'task', task): False for tenant, task in tasks})
    resource_owners.update({(tenant, 'deployment', deployment): deleted for tenant, deployment, deleted in deployments})
    run_owners.update({(tenant, 'deployment-run-' + deployment): ('deployment', deployment, deleted)
                       for tenant, deployment, deleted in deployments})
    run_owners.update({(tenant, 'deployment-run-' + deployment.replace('-', '')): ('deployment', deployment, deleted)
                       for tenant, deployment, deleted in deployments})
    run_owners.update({(tenant, project_workspace_scope_id(project)):
                      ('project', project_workspace_scope_id(project), deleted)
                      for tenant, _user, project, deleted in projects})
    run_owners.update({(tenant, 'task-run-' + task): ('task', task, False) for tenant, task in tasks})
    scheduled_owners = {(tenant, execution): ('task', task, False)
                        for tenant, execution, task in scheduled}
    execution_index = {(tenant, execution): (kind, resource)
                       for tenant, execution, kind, resource in executions}
    for tenant, execution, kind, resource in executions:
        owner_key = (tenant, kind, resource)
        if owner_key in resource_owners:
            run_owners[(tenant, execution)] = (kind, resource, resource_owners[owner_key])
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as destination:
        def emit(row):
            destination.write(json.dumps(row, ensure_ascii=False) + '\n')
            counts[row['status']] += 1

        for source, tenant, scope, path, key, size in files:
            owner = owners.get((tenant, scope))
            if scope in mounts:
                owner = ('user_mount', mounts[scope], False)
            prefix = '/mount/' if owner and owner[0] == 'user_mount' else '/'
            relative = path[len(prefix):] if path.startswith(prefix) else ''
            valid = relative and all(p not in {'', '.', '..'} for p in relative.split('/')) and '\0' not in relative
            status = ('unresolved' if owner is None or not valid or not key else
                      'deleted_resource' if owner[2] else 'ready')
            # These are projections of versioned packages, not chat workspace
            # files. Runtime Skills are rebuilt from database revision files.
            if scope in skill_scopes and path.startswith('/skills/'):
                status = 'skill_package_projection'
            emit(dict(category=source, tenant_id=tenant, scope_id=scope, path=path,
                      source_key=key, size_bytes=size, status=status,
                      kind=owner[0] if owner else None, resource_id=owner[1] if owner else None,
                      relative_path=relative))
        destinations = Counter()
        run_rows = []
        for tenant, run, path, key, size, modified in runs:
            owner = run_owners.get((tenant, run))
            if owner is None and run.startswith('schedule-'):
                # Old workers used schedule-{execution UUID}-{claim token}.
                owner = scheduled_owners.get((tenant, run[len('schedule-'):len('schedule-') + 36]))
            relative = path.removeprefix('/run/') if path.startswith('/run/') else ''
            valid = relative and all(p not in {'', '.', '..'} for p in relative.split('/')) and '\0' not in relative
            status = 'unresolved' if not owner or not valid else 'deleted_resource' if owner[2] else 'ready'
            if status == 'unresolved' and valid:
                if run.startswith('eval-'):
                    # Evaluation metrics live in Task records/logs. Its sandbox
                    # workspace was temporary and has no persistent resource.
                    try:
                        uuid.UUID(run.removeprefix('eval-'))
                    except ValueError:
                        pass
                    else:
                        status = 'retain_evaluation_archive'
                else:
                    try:
                        uuid.UUID(run)
                    except ValueError:
                        pass
                    else:
                        execution_owner = execution_index.get((tenant, run))
                        if execution_owner is None:
                            status = 'retain_unlinked_execution_archive'
                        elif any((kind, resource) == execution_owner and other_tenant != tenant
                                 for other_tenant, kind, resource in resource_owners):
                            # Never publish a user's old private execution files
                            # into the shared owner's directory during migration.
                            status = 'retain_cross_tenant_execution_archive'
            row = dict(category='run', tenant_id=tenant, run_id=run, path=path, source_key=key,
                       size_bytes=size, relative_path=relative, status=status,
                       kind=owner[0] if owner else None, resource_id=owner[1] if owner else None,
                       current_workflow_run=bool(owner and owner[0] == 'workflow'
                                                 and (tenant, owner[1], run) in current_runs),
                       modified_at=modified.isoformat() if modified else None)
            run_rows.append(row)
            if status == 'ready':
                destinations[(tenant, owner[0], owner[1], relative)] += 1
        for row in run_rows:
            coordinate = (row['tenant_id'], row['kind'], row['resource_id'], row['relative_path'])
            if row['status'] == 'ready' and destinations[coordinate] > 1:
                row['status'] = 'run_destination_collision'
            emit(row)
        store = get_object_store()
        task_tenants = {task: tenant for tenant, task in tasks}
        for key in store.list_keys('tasks/'):
            parts = key.split('/')
            task = parts[1] if len(parts) >= 3 else None
            tenant = task_tenants.get(task)
            emit(dict(category='task_result', tenant_id=tenant, resource_id=task,
                      source_key=key, relative_path=key,
                      status='ready' if tenant is not None else 'retain_deleted_task_result_archive'))
        for tenant, user, project, deleted in projects:
            scope = project_workspace_scope_id(project)
            volume = _volume_scope(tenant, user, scope)
            prefix = f'project-runtime-v1/{tenant}/{user}/{volume}/'
            for key in store.list_keys(prefix):
                relative = key[len(prefix):]
                valid = relative and all(p not in {'', '.', '..'} for p in relative.split('/')) and '\0' not in relative
                emit(dict(category='runtime', tenant_id=tenant, user_id=user,
                          project_scope_id=scope, source_key=key, relative_path=relative,
                          status='unresolved' if not valid else 'deleted_resource' if deleted else 'ready'))
        destination.flush()
        os.fsync(destination.fileno())
    return dict(counts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New private JSONL inventory file')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(inventory(args.output))))
