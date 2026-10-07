"""Standalone Linux integration probe; run under with_cgroup_delegation.py.

Uses at most 128 MiB per worker and 0.1 CPU. No workflow, database, or live
deployment is accessed. The delegated transient unit owns all test processes.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from vibecanvas_api.services.sandbox.resource_limits import ResourceAllocator, ResourceBudget


def main():
    detected = ResourceAllocator.from_environment()
    assert detected.memory_capacity_mb <= 192, 'ancestor MemoryMax was ignored'
    allocator = ResourceAllocator(Path(os.environ['SANDBOX_CGROUP_ROOT']),
        cpu_capacity_millis=200, memory_capacity_mb=256)
    first, second, third = (str(uuid.uuid4()) for _ in range(3))
    group = allocator.reserve(first, ResourceBudget(cpu_millis=100, memory_mb=128))
    allocator.reserve(second, ResourceBudget(cpu_millis=100, memory_mb=128))
    try:
        allocator.reserve(third, ResourceBudget(cpu_millis=100, memory_mb=128))
        raise AssertionError('rolling overlap exceeded capacity')
    except RuntimeError as exc:
        assert str(exc) == 'deployment_resource_capacity_exhausted'
    child = subprocess.Popen(group.wrap_command([sys.executable, '-c',
        'import time; payload=bytearray(16*1024*1024); end=time.monotonic()+2;\nwhile time.monotonic()<end: pass']))
    try:
        time.sleep(0.3)
        assert not allocator.release(first), 'released a live resource group'
        assert group.metrics()['memory_bytes'] > 0
        assert child.wait(timeout=8) == 0
        stats = group.metrics()
        assert 0 < stats['cpu_usage_usec'] < 700_000, stats
        assert (group.path / 'cpu.max').read_text().strip() == '10000 100000'
        assert (group.path / 'memory.max').read_text().strip() == str(128 * 1024 * 1024)
        memory_child = subprocess.run(group.wrap_command([sys.executable, '-c',
            'payload=bytearray(192*1024*1024)']), timeout=10)
        assert memory_child.returncode < 0, memory_child.returncode
        events = dict(line.split() for line in (group.path / 'memory.events').read_text().splitlines())
        assert int(events['oom_kill']) >= 1, events
        print(json.dumps({'cpu_quota_verified': True, 'memory_oom_enforced': True,
            'overlap_admission_verified': True, 'cpu_usage_usec': stats['cpu_usage_usec']}))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        assert allocator.release(first)
        assert allocator.release(second)
    assert not list(allocator.root.glob('deployment-*'))
    allocator.reserve(third, ResourceBudget(cpu_millis=100, memory_mb=128))
    recovered = ResourceAllocator(allocator.root, cpu_capacity_millis=200, memory_capacity_mb=256)
    assert recovered.release(third), 'empty group from a previous daemon was not reclaimed'
    assert not list(allocator.root.glob('deployment-*'))
    allocator.root.rmdir()
    recreated = allocator.reserve(third, ResourceBudget(cpu_millis=100, memory_mb=128))
    assert (recreated.path / 'cpu.max').read_text().strip() == '10000 100000'
    assert (recreated.path / 'memory.max').read_text().strip() == str(128 * 1024 * 1024)
    assert allocator.release(third)


if __name__ == '__main__':
    main()
