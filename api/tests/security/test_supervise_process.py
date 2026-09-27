"""Exercise real supervisor shutdown, including a child that ignores SIGTERM."""
from pathlib import Path
import os
import select
import signal
import subprocess
import sys

import pytest


@pytest.mark.parametrize("ignore_term", [False, True])
def test_supervisor_reaps_its_child_before_exiting(ignore_term):
    supervisor = Path(__file__).resolve().parents[3] / "scripts/supervise_process.py"
    code = (
        "import os,signal,time; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
        + "print(os.getpid(), flush=True); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, str(supervisor), "--stop-timeout", "0.2", "--", sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    child_pid = None
    try:
        assert select.select([process.stdout], [], [], 5)[0], "child did not start"
        child_pid = int(process.stdout.readline())
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert not stdout, "supervisor restarted a child during shutdown"
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
