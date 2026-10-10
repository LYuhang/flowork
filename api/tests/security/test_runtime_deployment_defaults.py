"""Keep native, direct, Compose and release entrypoints on the same runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from vibecanvas_api.config import AppConfig

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = {
    'SANDBOX_RUNTIME': 'bubblewrap',
    'SANDBOX_TYPE': 'rootless-warm',
    'DBOS_MAX_EXECUTOR_THREADS': '2',
    'BACKGROUND_QUEUE_CONCURRENCY': '1',
    'SANDBOX_MAX_RESIDENT': '2',
}


def test_direct_startup_matches_env_template(monkeypatch):
    for key in DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    config = AppConfig({})
    values = dict(line.split('=', 1) for line in (ROOT / '.env.example').read_text().splitlines()
                  if line and not line.startswith('#') and '=' in line)
    for key, expected in DEFAULTS.items():
        assert values[key] == expected
    assert config.sandbox_runtime == DEFAULTS['SANDBOX_RUNTIME']
    assert config.sandbox_type == DEFAULTS['SANDBOX_TYPE']
    assert config.dbos_max_executor_threads == 2
    assert config.background_queue_concurrency == 1
    assert config.sandbox_max_resident == 2


def test_release_overlay_cannot_introduce_different_defaults():
    base = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    release = yaml.safe_load((ROOT / 'docker-compose.release.yml').read_text())
    for key in ('SANDBOX_RUNTIME', 'SANDBOX_TYPE'):
        expected = '${' + key + ':-' + DEFAULTS[key] + '}'
        assert base['services']['sandboxd']['environment'][key] == expected
        for service in release['services'].values():
            value = service.get('environment', {}).get(key)
            assert value is None or value == expected
    worker = base['services']['background_worker']
    assert worker['command'] == ['python', '-m', 'vibecanvas_api.background_worker']
    for key in ('DBOS_MAX_EXECUTOR_THREADS', 'BACKGROUND_QUEUE_CONCURRENCY'):
        assert worker['environment'][key] == '${' + key + ':-' + DEFAULTS[key] + '}'
    assert not {'celery', 'celery_worker', 'celery_beat', 'temporal'} & base['services'].keys()


def test_unconfigured_provider_resolves_bubblewrap(monkeypatch):
    from vibecanvas_api.config import config
    from vibecanvas_api.services import sandbox
    monkeypatch.delattr(config, 'sandbox_runtime')
    monkeypatch.setattr(sandbox, '_resolve_bwrap', lambda: '/test/bwrap')
    monkeypatch.setattr(sandbox, '_resolve_runsc', lambda: pytest.fail('Default attempted gVisor'))
    assert isinstance(sandbox.get_sandbox_provider(), sandbox.BubblewrapProvider)


@pytest.mark.parametrize('explicit', [False, True])
def test_rendered_release_preserves_runtime_selection(explicit):
    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI unavailable')
    env = {k: v for k, v in os.environ.items() if k not in DEFAULTS}
    env.update({
        'VIBECANVAS_ENV_FILE': str(ROOT / '.env.example'),
        'OPENFGA_API_URL': 'https://openfga.example.test',
        'OPENFGA_API_TOKEN': 'test-only', 'OPENFGA_STORE_ID': 'test-store',
        'OPENFGA_AUTHORIZATION_MODEL_ID': 'test-model', 'OPENFGA_MODEL_SHA256': '0' * 64,
    })
    for name in ('API', 'SANDBOX', 'WEB', 'POSTGRES', 'OPENFGA_POSTGRES', 'OPENFGA', 'VALKEY'):
        env[f'VIBECANVAS_{name}_IMAGE'] = 'example.invalid/flowork-' + name.lower() + '@sha256:' + '0' * 64
    if explicit:
        env.update(SANDBOX_RUNTIME='gvisor', SANDBOX_TYPE='rootful-snapshot')
    command = ['docker', 'compose', '--env-file', str(ROOT / '.env.example'),
               '-f', str(ROOT / 'docker-compose.yml')]
    def render(args):
        result = subprocess.run(args + ['config', '--format', 'json'], env=env, cwd=ROOT,
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout)['services']
    base = render(command)
    release = render(command + ['-f', str(ROOT / 'docker-compose.release.yml')])
    assert release['openfga']['image'] == env['VIBECANVAS_OPENFGA_IMAGE']
    assert release['openfga_migrate']['image'] == env['VIBECANVAS_OPENFGA_IMAGE']
    assert release['openfga_postgres']['image'] == env['VIBECANVAS_OPENFGA_POSTGRES_IMAGE']
    assert release['openfga_postgres']['volumes'] == base['openfga_postgres']['volumes']
    sandbox = release['sandboxd']['environment']
    for key in ('SANDBOX_RUNTIME', 'SANDBOX_TYPE'):
        expected = env.get(key, DEFAULTS[key])
        assert base['sandboxd']['environment'][key] == sandbox[key] == expected
        assert release['api']['environment'][key] == expected
        assert release['background_worker']['environment'][key] == expected
    for key in ('DBOS_MAX_EXECUTOR_THREADS', 'BACKGROUND_QUEUE_CONCURRENCY'):
        assert str(release['background_worker']['environment'][key]) == DEFAULTS[key]


@pytest.mark.parametrize('backend', ['bubblewrap', 'gvisor'])
def test_sandbox_rpc_ref_reports_actual_selected_backend(monkeypatch, backend):
    from types import SimpleNamespace
    from vibecanvas_api.config import config
    from vibecanvas_api.services.sandbox.service import _SandboxGrpcService
    from vibecanvas_api.services.sandbox.proto import sandbox_service_pb2 as pb
    monkeypatch.setattr(config, 'sandbox_runtime', backend)
    service = _SandboxGrpcService(SimpleNamespace(socket_path='/tmp/test.sock', generation=1))
    ref = service._ref(pb.SandboxScope(tenant_id='test', scope_id='test'))
    assert ref.provider == backend + '-local'


@pytest.mark.parametrize('backend', ['object_store', 'posix'])
def test_compose_initializers_and_gateway_share_storage_configuration(backend, tmp_path):
    """POSIX selection must not prevent config import in auxiliary services."""
    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI unavailable')
    env = dict(os.environ, VIBECANVAS_ENV_FILE=str(ROOT / '.env.example'),
               WORKSPACE_STORAGE_BACKEND=backend, WORKSPACE_STORAGE_HOST_ROOT=str(tmp_path))
    command = ['docker', 'compose', '--env-file', str(ROOT / '.env.example'),
               '-f', str(ROOT / 'docker-compose.yml')]
    if backend == 'posix':
        command += ['-f', str(ROOT / 'docker-compose.posix.yml')]
    result = subprocess.run(command + ['config', '--format', 'json'], env=env, cwd=ROOT,
                            check=True, capture_output=True, text=True)
    services = json.loads(result.stdout)['services']
    for name in ('api', 'background_worker', 'sandboxd', 'migrate',
                 'sandbox_prewarm', 'browser_gateway'):
        settings = services[name]['environment']
        assert settings['WORKSPACE_STORAGE_BACKEND'] == backend, name
        assert settings['WORKSPACE_STORAGE_ROOT'] == '/var/lib/vibecanvas/workspaces', name
