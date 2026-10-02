"""Encrypted package snapshots; version zero is the one shared editing draft.

Callers hold the Knowledge identity row lock for every write. Published bytes
are independent of index-file GC and are never exposed to search from a draft.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from sqlalchemy import text

from vibecanvas_api.security.content_encryption import content_encryption_service


def package_hash(files, base_version):
    digest = hashlib.sha256(str(base_version).encode())
    for item in sorted(files, key=lambda item: item.path):
        digest.update(json.dumps([item.path, item.content_type, hashlib.sha256(item.data).hexdigest()]).encode())
    return digest.hexdigest()


async def snapshot_row(session, kb_id, version):
    result = await session.execute(text('SELECT * FROM knowledge_package_snapshots WHERE kb_id=:id AND version=:v'), {'id': kb_id, 'v': version})
    return result.mappings().one_or_none()


async def save_snapshot(session, kb, files, *, version, base_version):
    existing = await snapshot_row(session, kb.id, version)
    if version and existing:
        return existing
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps([{'path': item.path, 'content_type': item.content_type} for item in files]))
        for index, item in enumerate(files):
            archive.writestr(str(index), item.data)
    encrypted = await content_encryption_service().encrypt_bytes(
        session, tenant_id=kb.tenant_id, resource_type='knowledge_base', resource_id=str(kb.id),
        purpose='knowledge_package_snapshot', record_id=f'{kb.id}:{version}', plaintext=output.getvalue())
    await session.execute(text('''INSERT INTO knowledge_package_snapshots
        (kb_id,tenant_id,version,base_version,content_hash,file_count,size_bytes,content_ciphertext,content_nonce,content_key_id)
        VALUES (:id,:tenant,:version,:base,:hash,:count,:size,:cipher,:nonce,:key)
        ON CONFLICT (kb_id,version) DO UPDATE SET base_version=EXCLUDED.base_version,
        content_hash=EXCLUDED.content_hash,file_count=EXCLUDED.file_count,size_bytes=EXCLUDED.size_bytes,
        content_ciphertext=EXCLUDED.content_ciphertext,content_nonce=EXCLUDED.content_nonce,
        content_key_id=EXCLUDED.content_key_id,updated_at=now() WHERE knowledge_package_snapshots.version=0'''),
        {'id':kb.id,'tenant':kb.tenant_id,'version':version,'base':base_version,
         'hash':package_hash(files,base_version),'count':len(files),'size':sum(len(item.data) for item in files),
         'cipher':encrypted.ciphertext,'nonce':encrypted.nonce,'key':encrypted.key_id})
    return await snapshot_row(session, kb.id, version)


async def read_snapshot(session, kb_id, version):
    from vibecanvas_api.services.knowledge_packages import PackageFile
    row = await snapshot_row(session, kb_id, version)
    if row is None:
        return None
    blob = await content_encryption_service().decrypt_bytes(
        session, key_id=row['content_key_id'], tenant_id=row['tenant_id'], resource_type='knowledge_base',
        resource_id=str(kb_id), purpose='knowledge_package_snapshot', record_id=f'{kb_id}:{version}',
        ciphertext=row['content_ciphertext'], nonce=row['content_nonce'])
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return [PackageFile(item['path'], archive.read(str(index)), item['content_type'])
                for index,item in enumerate(json.loads(archive.read('manifest.json')))]


async def archive_current(session, kb):
    from vibecanvas_api.services.knowledge_packages import package_snapshot
    if await snapshot_row(session, kb.id, kb.package_version) is None:
        files = await package_snapshot(session, kb.id)
        if files:
            await save_snapshot(session, kb, files, version=kb.package_version, base_version=kb.package_version)


async def clear_draft(session, kb_id):
    await session.execute(text('DELETE FROM knowledge_package_snapshots WHERE kb_id=:id AND version=0'), {'id':kb_id})
