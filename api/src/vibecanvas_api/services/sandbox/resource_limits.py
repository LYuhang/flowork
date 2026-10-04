"""Delegated cgroup v2 resource limits for resident deployment instances.

The launcher joins before exec/fork, so every sandbox descendant inherits the
limits. No threaded preexec_fn and no post-spawn PID migration race.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResourceBudget:
    cpu_millis: int = 500
    memory_mb: int = 256

    def __post_init__(self):
        if type(self.cpu_millis) is not int or not 100 <= self.cpu_millis <= 256_000:
            raise ValueError('cpu_millis must be an integer between 100 and 256000')
        if type(self.memory_mb) is not int or not 128 <= self.memory_mb <= 1_048_576:
            raise ValueError('memory_mb must be an integer between 128 and 1048576')


class ResourceGroup:
    def __init__(self, path: Path, budget: ResourceBudget):
        self.path, self.budget = path, budget
        self.started_at = time.time()
        self._sample: tuple[float, int] | None = None

    def wrap_command(self, command: list[str]) -> list[str]:
        # Both the script and the cgroup path are host-owned. The guest cannot
        # change membership: the cgroup filesystem is not mounted writable.
        launcher = "import os,sys; p=sys.argv[1]; f=open(p,'w'); f.write(str(os.getpid())); f.close(); os.execvp(sys.argv[2],sys.argv[2:])"
        return [sys.executable, '-c', launcher, str(self.path / 'cgroup.procs'), *command]

    def metrics(self) -> dict:
        try:
            memory = int((self.path / 'memory.current').read_text())
            cpu = dict(line.split() for line in (self.path / 'cpu.stat').read_text().splitlines())
            usage = int(cpu['usage_usec'])
            now = time.monotonic()
            previous, self._sample = self._sample, (now, usage)
            percent = None
            if previous and now > previous[0] and usage >= previous[1]:
                # 100% = one fully used CPU, not a percentage of the quota.
                percent = round((usage - previous[1]) / (now - previous[0]) / 10_000, 2)
            return {'memory_bytes': memory, 'cpu_percent': percent,
                    'cpu_usage_usec': usage, 'started_at_unix': self.started_at,
                    'uptime_seconds': max(0, time.time() - self.started_at),
                    'cpu_millis': self.budget.cpu_millis, 'memory_mb': self.budget.memory_mb}
        except (OSError, ValueError, KeyError):
            return {'memory_bytes': None, 'cpu_percent': None}

    def release(self) -> bool:
        if not self.path.exists():
            return True
        events = dict(line.split() for line in (self.path / 'cgroup.events').read_text().splitlines())
        if events.get('populated') != '0':
            return False
        self.path.rmdir()
        return True


class ResourceAllocator:
    """Single-daemon admission; existing cgroups also count after a restart."""
    def __init__(self, root: Path, *, cpu_capacity_millis: int, memory_capacity_mb: int):
        self.root = root
        self.cpu_capacity_millis = cpu_capacity_millis
        self.memory_capacity_mb = memory_capacity_mb
        self._lock = threading.Lock()
        self._groups: dict[str, ResourceGroup] = {}
        self._pending_releases: dict[str, ResourceGroup] = {}

    @classmethod
    def from_environment(cls):
        configured = os.environ.get('SANDBOX_CGROUP_ROOT')
        if not configured:
            raise RuntimeError('deployment_resource_delegation_unavailable')
        root = Path(configured).resolve()
        cgroup_root = Path('/sys/fs/cgroup')
        if cgroup_root not in root.parents:
            raise RuntimeError('deployment_resource_delegation_invalid')
        cpu = len(os.sched_getaffinity(0)) * 1000
        memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') // (1024 * 1024)
        for ancestor in (root, *root.parents):
            if ancestor == cgroup_root:
                break
            cpu_file, memory_file = ancestor / 'cpu.max', ancestor / 'memory.max'
            if cpu_file.exists():
                quota, period = cpu_file.read_text().split()
                if quota != 'max':
                    cpu = min(cpu, int(quota) * 1000 // int(period))
            if memory_file.exists():
                limit = memory_file.read_text().strip()
                if limit != 'max':
                    memory = min(memory, int(limit) // (1024 * 1024))
        # Default headroom for API, DB and interactive sessions; operators can
        # explicitly budget this deployment pool within the host/parent limits.
        cpu_budget = int(os.environ.get('SANDBOX_DEPLOYMENT_CPU_MILLIS', cpu * 3 // 4))
        memory_budget = int(os.environ.get('SANDBOX_DEPLOYMENT_MEMORY_MB', memory // 2))
        if not 100 <= cpu_budget <= cpu or not 128 <= memory_budget <= memory:
            raise RuntimeError('deployment_resource_capacity_invalid')
        return cls(root, cpu_capacity_millis=cpu_budget, memory_capacity_mb=memory_budget)

    def reserve(self, revision_id: str, budget: ResourceBudget) -> ResourceGroup:
        name = 'deployment-' + uuid.UUID(str(revision_id)).hex
        with self._lock:
            if not {'cpu', 'memory', 'pids'}.issubset(set((self.root / 'cgroup.subtree_control').read_text().split())):
                raise RuntimeError('deployment_resource_delegation_unavailable')
            # A confirmed stop can lag a failed preparation/retirement. Reap
            # only groups whose owner explicitly requested release; an empty
            # active reservation may still be about to launch its first child.
            for pending_name, pending in list(self._pending_releases.items()):
                if pending.release():
                    self._pending_releases.pop(pending_name, None)
                    self._groups.pop(pending_name, None)
            if name in self._pending_releases:
                raise RuntimeError('deployment_resource_instance_still_running')
            if name in self._groups:
                group = self._groups[name]
                if group.budget != budget:
                    raise RuntimeError('deployment_resource_budget_changed')
                return group
            cpu_used, memory_used = 0, 0
            for child in self.root.glob('deployment-*'):
                # Refuse to overwrite an orphaned group's limits or attach a
                # second supervisor to live processes after an abnormal exit.
                if child.name not in self._groups:
                    orphan = ResourceGroup(child, budget)
                    if orphan.release():
                        continue
                    if child.name == name:
                        raise RuntimeError('deployment_resource_instance_still_running')
                quota, period = (child / 'cpu.max').read_text().split()
                if quota == 'max':
                    raise RuntimeError('deployment_resource_unbounded_instance')
                cpu_used += int(quota) * 1000 // int(period)
                memory_used += int((child / 'memory.max').read_text()) // (1024 * 1024)
            if cpu_used + budget.cpu_millis > self.cpu_capacity_millis or memory_used + budget.memory_mb > self.memory_capacity_mb:
                raise RuntimeError('deployment_resource_capacity_exhausted')
            path = self.root / name
            path.mkdir()
            try:
                (path / 'cpu.max').write_text(f'{budget.cpu_millis * 100} 100000')
                (path / 'memory.max').write_text(str(budget.memory_mb * 1024 * 1024))
                (path / 'memory.swap.max').write_text('0')
                (path / 'memory.oom.group').write_text('1')
                (path / 'pids.max').write_text('512')
            except BaseException:
                path.rmdir()
                raise
            group = ResourceGroup(path, budget)
            self._groups[name] = group
            return group

    def release(self, revision_id: str) -> bool:
        name = 'deployment-' + uuid.UUID(str(revision_id)).hex
        with self._lock:
            group = self._groups.get(name) or ResourceGroup(self.root / name, ResourceBudget())
            if not group.release():
                self._pending_releases[name] = group
                return False
            self._pending_releases.pop(name, None)
            self._groups.pop(name, None)
            return True
