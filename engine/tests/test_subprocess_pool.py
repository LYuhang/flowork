# -*- coding: utf-8 -*-
"""BoundedSubprocessPool — the generic, engine-agnostic worker pool skeleton
extracted from code_runner (A1). Pure stdlib; the worker script is pluggable so
both CodeNode (code_worker.py) and the sandbox parallel serve (job_worker.py)
reuse one pool.
"""
import os
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor

from vibecanvas_engine.subprocess_pool import BoundedSubprocessPool

# A trivial echo worker (stdlib-only): read one framed job dict, reply
# {"status": "success", "output": {"echo": <job>}}.
_ECHO = textwrap.dedent('''
    import os, sys, struct, json
    _LEN = struct.Struct(">I")
    jr, rw = int(sys.argv[1]), int(sys.argv[2])
    def _read(fd, n):
        buf = b""
        while len(buf) < n:
            c = os.read(fd, n - len(buf))
            if not c: raise EOFError
            buf += c
        return buf
    while True:
        try:
            n = _LEN.unpack(_read(jr, 4))[0]
            job = json.loads(_read(jr, n))
        except Exception:
            break
        body = json.dumps({"status": "success", "output": {"echo": job}}).encode()
        os.write(rw, _LEN.pack(len(body))); os.write(rw, body)
''')


def _env():
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def test_pool_runs_and_returns_in_order(tmp_path):
    script = tmp_path / "echo_worker.py"
    script.write_text(_ECHO)
    pool = BoundedSubprocessPool(worker_script=str(script), cwd=str(tmp_path),
                                 env=_env(), max_workers=2)
    try:
        r1 = pool.run({"a": 1}, timeout=5)
        r2 = pool.run({"a": 2}, timeout=5)
        assert r1["status"] == "success" and r1["output"]["echo"] == {"a": 1}
        assert r2["status"] == "success" and r2["output"]["echo"] == {"a": 2}
    finally:
        pool.close()


def test_pool_reuses_idle_worker(tmp_path):
    """Two sequential runs at max_workers=1 must reuse the single worker (no spawn storm)."""
    script = tmp_path / "echo_worker.py"
    script.write_text(_ECHO)
    spawned = {"n": 0}

    class _Counting(BoundedSubprocessPool):
        def _spawn(self):
            spawned["n"] += 1
            return super()._spawn()

    pool = _Counting(worker_script=str(script), cwd=str(tmp_path), env=_env(), max_workers=1)
    try:
        pool.run({"a": 1}, timeout=5)
        pool.run({"a": 2}, timeout=5)
        assert spawned["n"] == 1  # reused, not respawned
    finally:
        pool.close()


def test_pool_timeout_kills_and_recovers(tmp_path):
    hang = tmp_path / "hang_worker.py"
    # Reads the job then hangs forever (never replies) → parent read_result times out.
    hang.write_text(textwrap.dedent('''
        import os, sys, struct, time
        _LEN = struct.Struct(">I")
        jr = int(sys.argv[1])
        n = _LEN.unpack(os.read(jr, 4))[0]; os.read(jr, n)
        while True: time.sleep(1)
    '''))
    pool = BoundedSubprocessPool(worker_script=str(hang), cwd=str(tmp_path),
                                 env=_env(), max_workers=1)
    try:
        res = pool.run({"x": 1}, timeout=0.5)
        assert res["status"] == "error" and "timed out" in res["error_message"]
    finally:
        pool.close()


def test_custom_error_messages(tmp_path):
    hang = tmp_path / "hang2.py"
    hang.write_text(textwrap.dedent('''
        import os, sys, struct, time
        _LEN = struct.Struct(">I")
        jr = int(sys.argv[1])
        n = _LEN.unpack(os.read(jr, 4))[0]; os.read(jr, n)
        while True: time.sleep(1)
    '''))
    pool = BoundedSubprocessPool(worker_script=str(hang), cwd=str(tmp_path),
                                 env=_env(), max_workers=1)
    try:
        res = pool.run({"x": 1}, timeout=0.5, timeout_msg="my custom timeout")
        assert res["error_message"] == "my custom timeout"
    finally:
        pool.close()


def _kill_test_pool(tmp_path, max_workers):
    script = tmp_path / "kill_worker.py"
    script.write_text(_ECHO.replace(
        '    body = json.dumps',
        '    if job.get("hang"):\n'
        '        import time\n'
        '        open(job["started"], "w").close()\n'
        '        while True: time.sleep(0.01)\n'
        '    body = json.dumps',
    ))
    return BoundedSubprocessPool(worker_script=str(script), cwd=str(tmp_path),
                                 env=_env(), max_workers=max_workers, own_process_group=True)


def _wait_started(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()


def test_kill_one_worker_keeps_other_worker_and_pool_alive(tmp_path):
    pool = _kill_test_pool(tmp_path, 2)
    started = tmp_path / "started"
    kill_path = tmp_path / "worker.kill"
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(pool.run, {"hang": True, "started": str(started)},
                                  timeout=10, kill_path=str(kill_path))
        try:
            _wait_started(started)
            victim = next(iter(pool._busy))
            assert pool.run({"other": 1}, timeout=5)["status"] == "success"
            survivor = pool._idle[0]
            kill_path.touch()
            assert pending.result(timeout=5)["status"] == "cancelled"
            assert victim.proc.poll() is not None
            assert survivor.proc.poll() is None
            assert not pool._closed
            assert pool.run({"next": 2}, timeout=5)["output"]["echo"] == {"next": 2}
            assert pool._idle[0] is survivor
        finally:
            kill_path.touch()
            pool.close()


def test_killed_worker_is_replaced_on_next_job(tmp_path):
    pool = _kill_test_pool(tmp_path, 1)
    started = tmp_path / "started"
    kill_path = tmp_path / "worker.kill"
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(pool.run, {"hang": True, "started": str(started)},
                                  timeout=10, kill_path=str(kill_path))
        try:
            _wait_started(started)
            victim = next(iter(pool._busy))
            kill_path.touch()
            assert pending.result(timeout=5)["status"] == "cancelled"
            assert pool._total == 0
            assert pool.run({"next": 1}, timeout=5)["status"] == "success"
            assert pool._idle[0].proc.pid != victim.proc.pid
        finally:
            kill_path.touch()
            pool.close()


def test_kill_queued_job_never_runs_when_slot_becomes_free(tmp_path):
    pool = _kill_test_pool(tmp_path, 1)
    started = tmp_path / "started"
    first_kill = tmp_path / "first.kill"
    queued_kill = tmp_path / "queued.kill"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(pool.run, {"hang": True, "started": str(started)},
                                timeout=10, kill_path=str(first_kill))
        try:
            _wait_started(started)
            queued = executor.submit(pool.run, {"hang": True, "started": str(tmp_path / "never-started")},
                                     timeout=10, kill_path=str(queued_kill))
            queued_kill.touch()
            assert queued.result(timeout=5)["status"] == "cancelled"
            assert not (tmp_path / "never-started").exists()
            assert not first.done()
        finally:
            first_kill.touch()
            pool.close()
