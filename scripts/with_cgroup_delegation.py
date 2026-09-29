#!/usr/bin/env python3
"""systemd Delegate=yes entrypoint: place the supervisor in its own leaf.

Usage: python3 scripts/with_cgroup_delegation.py COMMAND [ARGS...]
Must run as the service user before the service forks workers. Requires a
writable delegated cgroup v2 subtree; never changes unrelated process groups.
"""
import os
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Expected a service command')
    membership = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    root = Path('/sys/fs/cgroup') / membership.lstrip('/')
    if root == Path('/sys/fs/cgroup'):
        raise SystemExit('A dedicated delegated service cgroup is required')
    processes = set((root / 'cgroup.procs').read_text().split())
    if processes - {str(os.getpid())}:
        raise SystemExit('Start this wrapper before other service processes')
    supervisor = root / 'supervisor'
    supervisor.mkdir(exist_ok=True)
    (supervisor / 'cgroup.procs').write_text(str(os.getpid()))
    (root / 'cgroup.subtree_control').write_text('+cpu +memory +pids')
    instances = root / 'instances'
    instances.mkdir(exist_ok=True)
    (instances / 'cgroup.subtree_control').write_text('+cpu +memory +pids')
    os.environ['SANDBOX_CGROUP_ROOT'] = str(instances)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
