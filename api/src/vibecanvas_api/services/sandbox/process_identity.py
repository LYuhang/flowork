"""Linux process identity and conservative proof that its process group is gone."""

import hashlib
from pathlib import Path
import socket


def host_identity() -> tuple[str, str]:
    machine = Path("/etc/machine-id").read_text().strip()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    host = hashlib.sha256((socket.gethostname() + "\0" + machine).encode()).hexdigest()
    return host, boot


def _stat(pid: int):
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return {"state": fields[0], "group": int(fields[2]), "start": int(fields[19])}


def capture_process(pid: int) -> dict:
    current = _stat(pid)
    if current is None or current["state"] == "Z" or current["group"] != pid:
        raise RuntimeError("workflow_process_identity_unavailable")
    host, boot = host_identity()
    return {"host_id": host, "boot_id": boot, "pid": pid, "group": current["group"], "start": current["start"]}


def process_group_gone(identity: dict) -> bool:
    """Never infer death from a timeout or kill a possibly reused PID."""
    try:
        host, boot = host_identity()
        if identity["host_id"] != host:
            return False
        if identity["boot_id"] != boot:
            return True  # All processes from the prior host boot have exited.
        pid, group = identity["pid"], identity["group"]
        if type(pid) is not int or pid <= 1 or group != pid:
            return False
        current = _stat(pid)
        if current is not None and current["start"] == identity["start"] and current["state"] != "Z":
            return False
        # The supervisor's disappearance alone is insufficient: children may
        # still be exiting. Reused group numbers are conservatively kept live.
        for path in Path("/proc").iterdir():
            if path.name.isdecimal():
                member = _stat(int(path.name))
                if member is not None and member["group"] == group and member["state"] != "Z":
                    return False
        return True
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False
