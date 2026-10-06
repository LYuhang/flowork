#!/usr/bin/env python3
"""Install a local encrypted POSIX volume, independent of application storage APIs.

Prerequisites on Ubuntu: apt-get install gocryptfs fuse3. Invoke as root, before
the offline workspace migration. For managed encrypted NFS/block volumes use
their normal mount service instead; this helper is for this single-host setup.
"""
import argparse
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess


def install(root: Path, user: str):
    if os.geteuid() != 0:
        raise ValueError('Run the volume installer as root')
    binary = shutil.which('gocryptfs')
    unmount = shutil.which('fusermount3')
    if not binary or not unmount:
        raise ValueError('Install gocryptfs and fuse3 first')
    if not root.is_absolute() or len(root.parts) < 4 or any(c.isspace() for c in str(root)):
        raise ValueError('Use a dedicated absolute mount path without whitespace')
    if any(c in str(root) for c in '%"\\\n'):
        raise ValueError('Unsupported mount path')
    account = pwd.getpwnam(user)
    cipher = root.with_name(root.name + '-encrypted')
    key = root.with_name(root.name + '-mount.key')
    for directory in (root, cipher):
        if directory.is_symlink():
            raise ValueError('Volume paths cannot be links')
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.path.ismount(root):
        raise ValueError('Stop the existing mount before reinstalling')
    if any(root.iterdir()):
        raise ValueError('Mount destination must be empty')
    configuration = cipher / 'gocryptfs.conf'
    if not configuration.exists():
        if any(cipher.iterdir()) or key.exists():
            raise ValueError('Refusing to initialize over existing volume data/key')
        descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(secrets.token_hex(32) + '\n')
            output.flush()
            os.fsync(output.fileno())
        subprocess.run([binary, '-q', '-init', '-passfile', str(key), str(cipher)],
                       check=True, stdout=subprocess.DEVNULL)
    if key.is_symlink() or not key.is_file():
        raise ValueError('Private mount key is missing')
    os.chown(key, 0, 0)
    key.chmod(0o600)
    os.chown(cipher, account.pw_uid, account.pw_gid)
    cipher.chmod(0o2770)
    # Inaccessible when unmounted: service must not silently write plaintext
    # to the underlying directory if the mount is lost.
    root.chmod(0o000)
    unit = f'''[Unit]
Description=Flowork encrypted POSIX workspace volume
Before=flowork.service

[Service]
Type=forking
ExecStart={binary} -q -nosyslog -allow_other -passfile {key} {cipher} {root}
ExecStop={unmount} -u {root}
TimeoutStartSec=60
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
'''
    Path('/etc/systemd/system/flowork-workspace-volume.service').write_text(unit)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'flowork-workspace-volume.service'], check=True)
    if not os.path.ismount(root):
        raise RuntimeError('Workspace volume did not mount')
    # Bind the running application only after the dependency is active.
    # Installing BindsTo before starting the mount stops a live application.
    dropins = Path('/etc/systemd/system/flowork.service.d')
    dropins.mkdir(exist_ok=True)
    (dropins / 'workspace-volume.conf').write_text(
        '[Unit]\nRequires=flowork-workspace-volume.service\n'
        'After=flowork-workspace-volume.service\nBindsTo=flowork-workspace-volume.service\n')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print(f'Volume mounted at {root}. Migrate and verify data before setting WORKSPACE_STORAGE_BACKEND=posix.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--user', default='flowork')
    args = parser.parse_args()
    install(args.root, args.user)
