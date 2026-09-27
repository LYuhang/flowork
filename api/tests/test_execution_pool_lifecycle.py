"""Batch-owned pools have a shorter lifetime than the resident sandbox."""
import json
import threading
from unittest.mock import Mock

import pytest

from vibecanvas_api import sandbox_entry as entry


def test_batches_reuse_only_their_own_pool_and_close_independently(tmp_path, monkeypatch):
    pools = []
    def build(*args):
        pool = Mock()
        pool.close.return_value = True
        pools.append(pool)
        return pool
    monkeypatch.setattr(entry, "_build_job_pool", build)
    holder, lock = {}, threading.Lock()
    first_id, other_id, next_id = "a" * 32, "b" * 32, "c" * 32
    def get(pool_id):
        return entry._get_parallel_workflow_pool(holder, lock, 4, str(tmp_path),
            execution_pool_id=pool_id, work_dir=str(tmp_path))
    first, other = get(first_id), get(other_id)
    assert get(first_id) is first and first is not other
    control = tmp_path / "pool-control"
    control.mkdir()
    (control / (first_id + ".stop")).touch()
    entry._stop_requested_pools(holder, lock, str(tmp_path))
    assert json.loads((control / (first_id + ".done")).read_text()) == {"closed": True}
    first.close.assert_called_once()
    other.close.assert_not_called()
    assert get(other_id) is other
    with pytest.raises(RuntimeError, match="stopped"):
        get(first_id)
    assert get(next_id) not in (first, other)
    # Repeated close controls do not kill replacement/unrelated pools.
    entry._stop_requested_pools(holder, lock, str(tmp_path))
    first.close.assert_called_once()
    assert len(pools) == 3


def test_stop_before_pool_creation_fences_late_rows(tmp_path, monkeypatch):
    build = Mock(side_effect=AssertionError("must not create stopped pool"))
    monkeypatch.setattr(entry, "_build_job_pool", build)
    holder, lock = {}, threading.Lock()
    control = tmp_path / "pool-control"
    control.mkdir()
    pool_id = "d" * 32
    (control / (pool_id + ".stop")).touch()
    entry._stop_requested_pools(holder, lock, str(tmp_path))
    with pytest.raises(RuntimeError, match="stopped"):
        entry._get_parallel_workflow_pool(holder, lock, 4, str(tmp_path),
            execution_pool_id=pool_id, work_dir=str(tmp_path))
    assert json.loads((control / (pool_id + ".done")).read_text())["closed"] is True
    build.assert_not_called()


def test_failed_pool_kill_is_not_acknowledged_as_closed(tmp_path):
    pool_id = "e" * 32
    pool = Mock()
    pool.close.return_value = False
    control = tmp_path / "pool-control"
    control.mkdir()
    (control / (pool_id + ".stop")).touch()
    entry._stop_requested_pools({"cli:" + pool_id: pool}, threading.Lock(), str(tmp_path))
    assert json.loads((control / (pool_id + ".done")).read_text()) == {"closed": False}
