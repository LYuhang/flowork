"""Exercise the scanner's admission boundary without Docker or network access."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
FAKE_TOOL = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
if name == 'docker':
    command = ' '.join(args)
    if 'cat /proc/1/comm' in command: print('postgres')
    elif 'SELECT count(*)' in command: print(2)
    elif 'SHOW server_version_num' in command: print(150019)
    elif 'image inspect' in command: print('sha256:' + 'a' * 64)
    sys.exit(0)
outputs = [a.split('=', 1)[1] for a in args if '=' in a]
label = pathlib.Path(outputs[0]).name.split('.')[0]
if name == 'syft':
    if label == os.environ.get('FAIL_SBOM'): sys.exit(1)
    for p in outputs: pathlib.Path(p).write_text('{}')
    sys.exit(0)
# All original upstream layers have a fixed High finding. Only an explicitly
# selected runtime label has one, allowing us to test both sides of admission.
bad = label in {'rust-build', 'python-base', 'node-runtime', 'node-build',
               'nginx-runtime', 'openfga-postgres-base', 'clamav-base', 'valkey-base', os.environ.get('FAIL_RUNTIME')}
report = {'matches': [{'vulnerability': {'id': 'CVE-2099-0001', 'severity': 'High',
    'fix': {'state': 'fixed', 'versions': ['2']}},
    'artifact': {'name': 'test-package', 'version': '1', 'type': 'deb'}}] if bad else []}
for p in outputs: pathlib.Path(p).write_text(json.dumps(report))
sys.exit(2 if bad else 0)
'''


@pytest.mark.parametrize('runtime,sbom,expected', [
    ('', '', 0), ('api', '', 2), ('openfga-postgres', '', 2),
    ('valkey', '', 2), ('node-build-patched', '', 2), ('clamav', '', 2), ('', 'python-base', 2),
])
def test_scan_blocks_actual_components_and_cannot_ignore_failed_sboms(tmp_path, runtime, sbom, expected):
    for name in ('docker', 'syft', 'grype'):
        tool = tmp_path / name
        tool.write_text(f'#!{sys.executable}\n' + FAKE_TOOL)
        tool.chmod(0o755)
    output = tmp_path / 'reports'
    result = subprocess.run(
        ['bash', str(ROOT / 'scripts/security/scan_container_images.sh'), str(output)],
        env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
             'DOCKER_BIN': str(tmp_path / 'docker'), 'SYFT_BIN': str(tmp_path / 'syft'),
             'GRYPE_BIN': str(tmp_path / 'grype'), 'PYTHON_BIN': sys.executable,
             'FAIL_RUNTIME': runtime, 'FAIL_SBOM': sbom},
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if not sbom:
        upstream = json.loads((output / 'vulnerabilities/python-base.json').read_text())
        assert upstream['matches'][0]['vulnerability']['severity'] == 'High'
        assert len(list((output / 'vulnerabilities').glob('*.json'))) == 19
