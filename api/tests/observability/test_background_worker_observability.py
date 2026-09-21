"""T7 — worker init configures logging + instruments + is fail-safe; multiproc
dir is cleared on init."""

from vibecanvas_api.observability import background_worker as worker_observability


def test_worker_init_is_fail_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    # seed a stale file to prove clear-on-init
    (tmp_path / "stale.db").write_text("x")
    worker_observability.init_worker_observability()  # must not raise
    assert not (tmp_path / "stale.db").exists()  # cleared


def test_worker_init_without_multiproc_dir_does_not_raise(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    worker_observability.init_worker_observability()  # no-op-ish, fail-safe
