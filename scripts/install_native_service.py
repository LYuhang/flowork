#!/usr/bin/env python3
"""Install the delegated native systemd service without restarting a live stack."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile


def quote(value: str, *, command: bool = False) -> str:
    if any(char in value for char in '\n\r\x00'):
        raise ValueError('Unit values cannot contain newlines or NUL')
    escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return '"' + (escaped.replace('$', '$$') if command else escaped) + '"'


def render(repo: Path, user: str, home: str, launch_env: Path) -> str:
    python = repo / '.venv/bin/python'
    launcher = repo / 'launch.sh'
    wrapper = repo / 'scripts/with_cgroup_delegation.py'
    search_path = f'{home}/.local/bin:{home}/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    return f'''[Unit]
Description=Flowork native application
Wants=network-online.target
After=network-online.target postgresql.service redis-server.service

[Service]
Type=oneshot
RemainAfterExit=yes
User={user}
WorkingDirectory={str(repo).replace('%', '%%')}
Environment={quote('PATH=' + search_path)}
Environment={quote('VIBECANVAS_LAUNCH_ENV=' + str(launch_env))}
Delegate=yes
ExecStart={quote(str(python), command=True)} {quote(str(wrapper), command=True)} {quote(str(launcher), command=True)} start
ExecStop={quote(str(launcher), command=True)} stop
KillMode=control-group
TimeoutStartSec=900
TimeoutStopSec=120
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True, help='Unprivileged service account')
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--launch-env', type=Path)
    parser.add_argument('--unit', default='flowork')
    parser.add_argument('--print', action='store_true', dest='print_only')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', args.user):
        parser.error('Invalid service user')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.unit):
        parser.error('Invalid service unit name')
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        parser.error('Run the application as an unprivileged user')
    repo = args.repo.resolve()
    launch_env = (args.launch_env or repo / '.env.launch.local').resolve()
    unit = render(repo, args.user, account.pw_dir, launch_env)
    if args.print_only:
        print(unit, end='')
        return
    if os.geteuid() != 0:
        parser.error('Installation requires sudo; use --print to preview')
    if not Path('/run/systemd/system').is_dir() or not Path('/sys/fs/cgroup/cgroup.controllers').is_file():
        parser.error('A running systemd host with cgroup v2 is required')
    for file in (repo / '.venv/bin/python', repo / 'launch.sh', repo / 'scripts/with_cgroup_delegation.py', launch_env):
        if not file.is_file():
            parser.error(f'Missing prepared installation file: {file}')
    # Validate the generated unit before replacing the installed definition.
    destination = Path('/etc/systemd/system') / (args.unit + '.service')
    with tempfile.TemporaryDirectory(prefix='flowork-unit-') as directory:
        staged = Path(directory) / destination.name
        staged.write_text(unit)
        subprocess.run(['systemd-analyze', 'verify', str(staged)], check=True)
        subprocess.run(['install', '-m', '0644', str(staged), str(destination)], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', destination.name], check=True)
    print(f'Installed {destination}. No running service was restarted.')
    print(f'Start with: sudo systemctl start {destination.name}')
    print('For an existing running stack, drain active work before restarting this unit.')


if __name__ == '__main__':
    main()
