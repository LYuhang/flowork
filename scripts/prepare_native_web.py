#!/usr/bin/env python3
"""Install locked frontend dependencies and build before restarting a native service."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import subprocess


def prepare(repo: Path, user: str, repair_ownership: bool = False) -> None:
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise ValueError('Use the unprivileged systemd service account')
    if os.geteuid() not in (0, account.pw_uid):
        raise PermissionError('Run as the service account or use sudo')
    web = repo.resolve() / 'web'
    for name in ('package.json', 'pnpm-lock.yaml'):
        if not (web / name).is_file():
            raise FileNotFoundError(web / name)
    prefix = ['runuser', '-u', user, '--'] if os.geteuid() == 0 else []
    env = {**os.environ, 'CI': 'true'}
    # Explicit per-command environment, no persistent root pnpm configuration.
    def run(args: list[str], **kwargs):
        return subprocess.run(prefix + args, cwd=web, env=env, check=True, **kwargs)
    if repair_ownership:
        if os.geteuid() != 0:
            raise PermissionError('--repair-ownership requires sudo')
        store = Path(run(['pnpm', 'store', 'path'], capture_output=True, text=True).stdout.strip()).resolve()
        if not store.is_relative_to(Path(account.pw_dir).resolve()):
            raise ValueError('Refusing to change a pnpm store outside the service account home')
        paths = [web / 'node_modules', web / 'dist', store]
        for path in paths:
            if path.is_symlink():
                raise ValueError(f'Refusing dependency/build symlink: {path}; prepare the actual deployment checkout')
            if path.exists():
                # -P prevents following nested package symlinks into unrelated trees.
                subprocess.run(['chown', '-R', '-P', f'{account.pw_uid}:{account.pw_gid}', str(path)], check=True)
    run(['pnpm', 'install', '--frozen-lockfile'])
    run(['pnpm', 'exec', 'vite', 'build'])
    run(['pnpm', 'run', 'lint:deployment-paths'])
    print('Frontend prepared. Running service was not stopped or restarted.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--user', required=True, help='Same account as the native systemd service')
    parser.add_argument('--repair-ownership', action='store_true', help='Repair dependency/build ownership left by a prior root installation')
    args = parser.parse_args()
    prepare(args.repo, args.user, args.repair_ownership)


if __name__ == '__main__':
    main()
