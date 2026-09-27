"""CLI hard-kill control is separate from workflow cooperative cancellation."""
from unittest.mock import Mock

import pytest

from vibecanvas_api.services.sandbox.warm import WarmGvisorPool


def test_kill_request_is_supervisor_only_and_does_not_restart_pool(tmp_path):
    pool = WarmGvisorPool(provider=Mock(), store_root=str(tmp_path),
                          work_root=str(tmp_path / "work"), tenant="tenant",
                          materialized_runs_root=str(tmp_path))
    pool._restart_worker = Mock(side_effect=AssertionError("must not restart pool"))
    pool._write_cancel_marker = Mock(side_effect=AssertionError("must not signal engine"))
    pool.kill_job(tenant="tenant", run_id="row0", run_subpath="cli/run/0")
    pool.kill_job(tenant="tenant", run_id="row0", run_subpath="cli/run/0")
    assert (tmp_path / "cli/run/0/__exec__/worker.kill").exists()
    assert not (tmp_path / "cli/run/0/__exec__/cancel").exists()


@pytest.mark.parametrize("tenant,path", [("other", "cli/run/0"), ("tenant", "../escape"), ("tenant", "/absolute"), ("tenant", "cli//row")])
def test_kill_scope_and_path_are_validated(tmp_path, tenant, path):
    pool = WarmGvisorPool(provider=Mock(), store_root=str(tmp_path),
                          work_root=str(tmp_path / "work"), tenant="tenant",
                          materialized_runs_root=str(tmp_path))
    with pytest.raises(ValueError):
        pool.kill_job(tenant=tenant, run_id="row", run_subpath=path)
    assert list(tmp_path.iterdir()) == []
