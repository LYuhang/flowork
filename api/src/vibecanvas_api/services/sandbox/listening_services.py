"""Discover and stop user services inside the Agent sandbox's PID/network namespace.

No registry or host PID is trusted: each observation joins listening socket
inodes to their live owners. Chat identity comes from the owner's ancestry.
"""
import asyncio
import os
from pathlib import Path
import re
import signal

_CHAT = re.compile(r"/chats/([0-9a-f-]{36})(?:/|$)")


def listening_services(proc: Path = Path('/proc')) -> list[dict]:
    sockets: dict[str, int] = {}
    for name in ('tcp', 'tcp6'):
        for line in (proc / 'net' / name).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A':
                sockets[fields[9]] = int(fields[1].rsplit(':', 1)[1], 16)
    namespace = os.readlink(proc / "self/ns/pid")
    processes = {}
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            if os.readlink(path / 'ns/pid') != namespace:
                continue
            status = (path / 'status').read_text().splitlines()
            local_pid = int(next(line for line in status if line.startswith('NSpid:')).split()[-1])
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            env = (path / 'environ').read_bytes().split(b'\0')
            chat = next((entry.split(b'=', 1)[1].decode() for entry in env
                         if entry.startswith(b'FLOWORK_SERVICE_CHAT_ID=')), None)
            if not chat:
                match = _CHAT.search(os.readlink(path / 'cwd'))
                chat = match.group(1) if match else None
            processes[int(path.name)] = (int(fields[1]), fields[19], chat, local_pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue  # A process may exit during this snapshot.
    services = []
    for pid, (parent, start, chat, local_pid) in processes.items():
        seen = {pid}
        while not chat and parent in processes and parent not in seen:
            seen.add(parent)
            parent, _, chat, _ = processes[parent]
        if not chat:
            continue  # Runtime infrastructure is not a user service.
        try:
            ports = set()
            for fd in (proc / str(pid) / 'fd').iterdir():
                try:
                    target = os.readlink(fd)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if target.startswith('socket:[') and target[8:-1] in sockets:
                    ports.add(sockets[target[8:-1]])
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        for port in sorted(ports):
            services.append({'port': port, 'pid': local_pid, 'start': start, 'chat_id': chat})
    return sorted(services, key=lambda item: (item["port"], item["pid"]))


async def stop_service(port: int, expected: list[dict]) -> list[dict]:
    current = listening_services()
    owners = [item for item in current if item['port'] == port]
    if not owners:
        return current
    if owners != expected:
        raise RuntimeError('service_changed_reopen_preview')
    # Signal socket owners, never a shared shell/runtime process group.
    for owner in owners:
        try:
            fd = os.pidfd_open(owner['pid'])
        except ProcessLookupError:
            continue
        try:
            # The bind-mounted /proc used by nested bubblewrap exposes host
            # PIDs. pidfd_open needs the namespace PID; fdinfo translates the
            # pinned process back into this procfs mount's numbering.
            info = Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
            proc_pid = int(next(line for line in info if line.startswith('Pid:')).split()[1])
            if proc_pid == -1:
                continue
            fields = Path(f"/proc/{proc_pid}/stat").read_text().rsplit(')', 1)[1].split()
            if fields[19] != owner['start']:
                raise RuntimeError('service_changed_reopen_preview')
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError):
            pass  # The pinned service exited while its identity was checked.
        finally:
            os.close(fd)
    deadline = asyncio.get_running_loop().time() + 3
    while True:
        current = listening_services()
        if not any(item['port'] == port for item in current):
            return current
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError('service_did_not_stop')
        await asyncio.sleep(0.1)
