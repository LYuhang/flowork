#!/usr/bin/env python3
"""Apply a fresh, conflict-free plan while all application writers are stopped.

The destination must already be a persistent mounted volume. Original objects
and database records are retained. This command does not switch application
configuration or start services. A failed run never produces a completion file.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from vibecanvas_api.config import config
from vibecanvas_api.security.crypto_core import local_master_key_from_config
from vibecanvas_api.security.posix_workspace_migration import migrate_file, migrate_encrypted_task_result
from vibecanvas_api.services.object_store import FilesystemObjectStore, get_object_store


def apply(plan_path: Path, root: Path, *, uid: int, gid: int) -> dict:
    plan = json.loads(plan_path.read_text())
    if plan.get('schema_version') != 1 or plan.get('conflicts'):
        raise ValueError('Migration requires a supported, conflict-free plan')
    if not root.is_absolute() or root.is_symlink() or not os.path.ismount(root):
        raise ValueError('Destination must be an existing mounted volume')
    complete = root / '.workspace-migration-complete.json'
    if complete.exists():
        raise ValueError('This volume already has a completed migration')
    source = get_object_store()
    target = FilesystemObjectStore(
        root=str(root / 'task-results-v1'),
        materialized_root=str(root / 'task-result-materializations'),
        master_key=local_master_key_from_config(require_persistent=True),
        encryption_chunk_bytes=config.object_store.fs_encryption_chunk_bytes,
    )
    count = size = 0
    for item in plan['files']:
        row = item['source']
        if row['category'] == 'task_result':
            result = migrate_encrypted_task_result(source, target, row['source_key'])
        else:
            result = migrate_file(source.iter_bytes(row['source_key']), root=root,
                                  relative=item['destination'], expected_size=row.get('size_bytes'))
        count += 1
        size += result.size_bytes
        if count % 500 == 0:
            print(json.dumps(dict(files=count, bytes=size)), flush=True)
    # Permission ownership is explicit for the deployment's trusted service
    # account/group. Sandbox mounts still expose only the admitted resource.
    for directory, _subdirs, files in os.walk(root, followlinks=False):
        path = Path(directory)
        relative = path.relative_to(root)
        shared = not relative.parts or relative.parts[0] in {'workspaces-v1', 'task-results-v1'}
        if path.is_symlink():
            raise ValueError('Unexpected link in migration volume')
        os.chown(path, uid, gid)
        path.chmod(0o2770 if shared else 0o700)
        for name in files:
            file = path / name
            if file.is_symlink():
                raise ValueError('Unexpected link in migration volume')
            os.chown(file, uid, gid)
            file.chmod(0o660 if shared else 0o600)
    report = dict(files=count, verified_bytes=size, source_objects_retained=True)
    descriptor = os.open(complete, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(report, output)
        output.flush()
        os.fsync(output.fileno())
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--uid', type=int, required=True)
    parser.add_argument('--gid', type=int, required=True)
    parser.add_argument('--writers-stopped', action='store_true', required=True,
                        help='Assert API, task workers and sandbox writers have been stopped')
    args = parser.parse_args()
    print(json.dumps(apply(args.plan, args.root, uid=args.uid, gid=args.gid)))
