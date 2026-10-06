#!/usr/bin/env python3
"""Resolve inventory destinations and content-identical duplicates, without writes.

Output is a private JSON plan. Differing sources remain explicit conflicts;
apply must reject any plan containing them. All unselected source objects stay
in the original encrypted store, which is not used as a runtime fallback.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path

from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.vfs_volume import LocalPosixProjectRuntimeVolumeProvider
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity


def destination(row: dict) -> str:
    # These resolvers are pure: no directories are acquired or created.
    root = Path('/workspace-migration-destination')
    if row['category'] == 'runtime':
        base = Path(LocalPosixProjectRuntimeVolumeProvider(str(root)).resolve(
            tenant_id=row['tenant_id'], user_id=row['user_id'],
            project_scope_id=row['project_scope_id']).path)
    elif row['category'] == 'task_result':
        base = root / 'task-results-v1'
    else:
        base = PosixWorkspaceStorage(str(root))._path(WorkspaceIdentity(
            row['tenant_id'], row['kind'], row['resource_id']))
    return (base / row['relative_path']).relative_to(root).as_posix()


def plan(inventory: Path) -> dict:
    groups = defaultdict(list)
    retained = []
    for line in inventory.read_text().splitlines():
        row = json.loads(line)
        if row['category'] == 'run' and row['relative_path'].split('/')[0] == '__exec__':
            retained.append({**row, 'status': 'retain_execution_transport_archive'})
            continue
        if row['status'] not in {'ready', 'run_destination_collision'}:
            retained.append(row)
            continue
        groups[destination(row)].append(row)
    store = get_object_store()
    files, conflicts, resolutions = [], [], []
    for target, candidates in sorted(groups.items()):
        unique = {row['source_key']: row for row in candidates}
        if len(unique) > 1:
            fingerprints = {}
            for key in unique:
                digest = hashlib.sha256()
                size = 0
                for chunk in store.iter_bytes(key):
                    digest.update(chunk)
                    size += len(chunk)
                fingerprints[key] = (size, digest.hexdigest())
            if len(set(fingerprints.values())) != 1:
                authoritative = [r for r in unique.values() if r['category'] in {'artifact', 'scratch'}]
                reason = 'current_workspace_record'
                if not authoritative and all(r['category'] == 'run' for r in unique.values()):
                    authoritative = [r for r in unique.values() if r.get('current_workflow_run')]
                    reason = 'current_workflow_run_state'
                if len(authoritative) == 1 and all(r['category'] in {'artifact', 'scratch', 'run'} for r in unique.values()):
                    # Project boot hydration used the artifact row as authority,
                    # not the incidental /run snapshot of the same file.
                    chosen = authoritative[0]
                    files.append(dict(destination=target, source=chosen, equivalent_source_keys=[chosen['source_key']]))
                    resolutions.append(dict(destination=target, reason=reason,
                                            chosen_source_key=chosen['source_key'], fingerprints=fingerprints))
                    retained.extend({**r, 'status': 'retain_superseded_run_snapshot'}
                                    for r in unique.values() if r is not chosen)
                    continue
                conflicts.append(dict(destination=target, candidates=list(unique.values()),
                                      fingerprints=fingerprints))
                continue
        chosen = next(iter(unique.values()))
        files.append(dict(destination=target, source=chosen,
                          equivalent_source_keys=sorted(unique)))
    return dict(schema_version=1, files=files, conflicts=conflicts, resolutions=resolutions, retained=retained,
                summary=dict(files=len(files), conflicts=len(conflicts),
                             resolutions=len(resolutions),
                             retained=dict(Counter(row['status'] for row in retained))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = plan(args.inventory)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(result, output)
    print(json.dumps(result['summary']))
