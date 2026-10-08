from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _installer():
    path = Path(__file__).resolve().parents[3] / 'scripts/security/install_encrypted_workspace_volume.py'
    spec = importlib.util.spec_from_file_location('workspace_installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('version', ['gocryptfs 2.4.0; go-fuse 2.4.2', 'gocryptfs v2.5.1;', 'unknown'])
def test_old_or_unrecognized_binary_cannot_initialize_workspace(monkeypatch, tmp_path, version):
    module = _installer()
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: version)
    root = tmp_path / 'workspace'
    with pytest.raises(ValueError, match='gocryptfs >= 2.6.1'):
        module.install(root, 'unused')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('version', ['gocryptfs v2.6.1;', 'gocryptfs 2.6.1; built with go', 'gocryptfs v2.7.0;'])
def test_fixed_release_accepted(monkeypatch, version):
    module = _installer()
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: version)
    module.check_gocryptfs_version('/usr/local/bin/gocryptfs')
