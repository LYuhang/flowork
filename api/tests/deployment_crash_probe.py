"""Child for the isolated Linux daemon-crash test; never run against live data."""
import asyncio
import base64
import json
import os
import sys
from pathlib import Path


async def main():
    if os.environ.get('FLOWORK_TEST_RESIDENT_CGROUP') != '1':
        raise RuntimeError('dedicated test unit required')
    data = json.load(sys.stdin)
    from vibecanvas_api.config import config
    from vibecanvas_api.services.sandbox.manager import SandboxManager
    config.sandbox_runtime = 'bubblewrap'
    config.sandbox_network = 'none'
    config.sandbox_fileop_workers = 1
    config.kms_provider = 'local'
    config.kms_local_master_key = base64.urlsafe_b64encode(b't' * 32).decode()
    config.kms_local_master_key_file = ''
    config.object_store.provider = 'filesystem'
    root = Path(data['root'])
    config.object_store.fs_root = str(root / 'objects')
    config.object_store.fs_materialized_root = str(root / 'plaintext')
    for name in ('agent_runtime_root', 'agent_overlay_root', 'vfs_volume_root'):
        setattr(config, name, str(root / name))
    manager = SandboxManager(max_resident=1, idle_ttl_s=60)
    async def hold(session, **kwargs):
        # A real bubblewrap instance and prewarmed engine already exist here.
        # Hold after durable claim/prepare so the parent can kill the daemon at
        # a deterministic point without relying on workflow-specific sleeps.
        Path(data['ready']).write_text(str(session._fileop_pool._handles[0].proc.pid))
        await asyncio.Event().wait()
    manager.deployments._execute_request = hold
    await manager.deployments.run(
        tenant_id=data['tenant'], deployment_id=data['deployment'], revision_id=data['revision'],
        workflow=data['workflow'], inputs={}, run_id=data['invocation'])


asyncio.run(main())
