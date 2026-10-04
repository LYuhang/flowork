"""Delayed child exit must free capacity without reclaiming active reservations."""
import shutil
from uuid import uuid4

import pytest

from vibecanvas_api.services.sandbox.resource_limits import ResourceAllocator, ResourceBudget, ResourceGroup


@pytest.fixture
def allocator(tmp_path, monkeypatch):
    (tmp_path / 'cgroup.subtree_control').write_text('cpu memory pids')
    live = set()

    def release(group):
        if group.path.name in live:
            return False
        if group.path.exists():
            shutil.rmtree(group.path)
        return True

    monkeypatch.setattr(ResourceGroup, 'release', release)
    return ResourceAllocator(tmp_path, cpu_capacity_millis=100, memory_capacity_mb=128), live


def test_delayed_release_reclaims_capacity_after_child_exits(allocator):
    pool, live = allocator
    first, second = str(uuid4()), str(uuid4())
    budget = ResourceBudget(cpu_millis=100, memory_mb=128)
    group = pool.reserve(first, budget)
    live.add(group.path.name)
    assert not pool.release(first)
    with pytest.raises(RuntimeError, match='capacity_exhausted'):
        pool.reserve(second, budget)
    with pytest.raises(RuntimeError, match='instance_still_running'):
        pool.reserve(first, budget)
    live.clear()
    replacement = pool.reserve(second, budget)
    assert replacement.path.exists() and not group.path.exists()
    assert list(pool._groups) == [replacement.path.name]


def test_empty_active_reservation_is_not_reclaimed(allocator):
    pool, _ = allocator
    first, second = str(uuid4()), str(uuid4())
    budget = ResourceBudget(cpu_millis=100, memory_mb=128)
    group = pool.reserve(first, budget)
    with pytest.raises(RuntimeError, match='capacity_exhausted'):
        pool.reserve(second, budget)
    assert pool.reserve(first, budget) is group
    assert pool.release(first)
    assert pool.reserve(second, budget).path.exists()


def test_delayed_orphan_release_is_retried(allocator):
    pool, live = allocator
    first, second = str(uuid4()), str(uuid4())
    budget = ResourceBudget(cpu_millis=100, memory_mb=128)
    orphan = pool.reserve(first, budget)
    recovered = ResourceAllocator(pool.root, cpu_capacity_millis=100, memory_capacity_mb=128)
    live.add(orphan.path.name)
    assert not recovered.release(first)
    live.clear()
    assert recovered.reserve(second, budget).path.exists()
    assert not orphan.path.exists()
