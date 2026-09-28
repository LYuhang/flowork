import statistics, time, pytest
from vibecanvas_api.services.sandbox import get_sandbox_provider, _gvisor_runnable
from vibecanvas_api.config import config

pytestmark = pytest.mark.skipif(not _gvisor_runnable(), reason="rootless gVisor not runnable here")

@pytest.fixture(autouse=True)
def select_gvisor_backend(monkeypatch):
    # These tests assert a gVisor kernel even when native launch uses Bubblewrap.
    monkeypatch.setattr(config, "sandbox_runtime", "gvisor")

def test_boots_gvisor_kernel(tmp_path):
    p = get_sandbox_provider()
    r = p.run(run_dir=str(tmp_path), command=["cat", "/proc/version"])
    assert r.exit_code == 0
    assert "gvisor" in r.stdout.lower()          # gVisor's userspace kernel, not host

def test_bind_mount_bidirectional(tmp_path):
    # host → inside
    (tmp_path / "host.txt").write_text("from-host")
    p = get_sandbox_provider()
    r = p.run(run_dir=str(tmp_path), command=["cat", "/run/host.txt"])
    assert r.exit_code == 0 and r.stdout.strip() == "from-host"
    # inside → host (the load-bearing P2 seam)
    p.run(run_dir=str(tmp_path), command=["sh", "-c", "echo from-sandbox > /run/inside.txt"])
    assert (tmp_path / "inside.txt").read_text().strip() == "from-sandbox"

def test_exit_code_and_stderr(tmp_path):
    r = get_sandbox_provider().run(run_dir=str(tmp_path), command=["sh","-c","echo E >&2; exit 7"])
    assert r.exit_code == 7 and "E" in r.stderr

def test_mount_bind_readable_inside(tmp_path):
    run_dir = tmp_path / "run"; run_dir.mkdir()
    mount_dir = tmp_path / "mount"; mount_dir.mkdir(parents=True)
    (mount_dir / "ref.csv").write_text("a,b\n1,2")
    p = get_sandbox_provider()
    r = p.run(run_dir=str(run_dir), command=["cat", "/mount/ref.csv"],
              extra_rw_binds=[("/mount", str(mount_dir))])
    assert r.exit_code == 0 and r.stdout.strip() == "a,b\n1,2"
    # cwd is still /run (the /run bind is first) — write lands in run_dir.
    p.run(run_dir=str(run_dir), command=["sh", "-c", "echo hi > here.txt"],
          extra_rw_binds=[("/mount", str(mount_dir))])
    assert (run_dir / "here.txt").read_text().strip() == "hi"


def test_run_without_extra_bind_has_no_mount(tmp_path):
    run_dir = tmp_path / "run"; run_dir.mkdir()
    p = get_sandbox_provider()
    r = p.run(run_dir=str(run_dir), command=["sh", "-c", "ls /mount 2>&1; true"])
    assert r.exit_code == 0
    assert "No such file" in r.stdout or "/mount" not in r.stdout


def test_overhead_floor(tmp_path):              # N2: recorded, not asserted
    p = get_sandbox_provider()
    p.run(run_dir=str(tmp_path), command=["true"])   # warm one
    times = []
    for _ in range(5):
        t = time.monotonic(); p.run(run_dir=str(tmp_path), command=["true"]); times.append(time.monotonic()-t)
    print(f"\n[RE-6 P1] gVisor cold single-shot boot+run floor: "
          f"min={min(times):.3f}s median={statistics.median(times):.3f}s "
          f"(NOT amortized warm-pool cost)")
    assert min(times) < 30   # sanity only
