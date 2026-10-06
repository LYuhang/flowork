"""Guest-local process activity; locks expire with their owning processes."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from uuid import UUID, uuid4


@contextmanager
def execution_activity(work_dir="/work", *, run_id=None):
    identifier = UUID(run_id).hex if run_id else uuid4().hex
    directory = Path(work_dir) / "local-executions"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = directory / ('.' + uuid4().hex)
    path = directory / identifier
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Publish only after acquiring the lock. An unlocked published marker
        # is therefore positive evidence that this command has exited.
        os.link(temporary, path)
        temporary.unlink()
        yield
    finally:
        temporary.unlink(missing_ok=True)
        os.close(fd)
        # Retain the published inode until sandbox teardown; the guest
        # supervisor can observe SIGKILL as well as an ordinary exit.


def active_executions(work_dir):
    """Observe locks inside the guest and publish confirmed-exit markers."""
    directory = Path(work_dir) / "local-executions"
    if not directory.exists():
        return 0
    finished = Path(work_dir) / 'local-execution-finished'
    finished.mkdir(mode=0o700, exist_ok=True)
    count = 0
    for path in directory.iterdir():
        if len(path.name) != 32 or any(char not in '0123456789abcdef' for char in path.name):
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                count += 1
            else:
                marker = finished / path.name
                if not marker.exists():
                    marker.touch(mode=0o600, exist_ok=True)
        finally:
            os.close(fd)
    return count
